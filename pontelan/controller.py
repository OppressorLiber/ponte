from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import platform
import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from .core import TunnelService
from .discovery import discover
from .update_manager import UpdateManager
from .event_loop import NetworkEventLoop


class Controller(QObject):
    event = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.loop = None
        self.service = None
        self.updater = None
        self.update_task = None
        self.pending_update_idle = False
        self.last_spike_capture = 0
        self.spike_capture_index = 0
        self.started = threading.Event()
        self.thread = threading.Thread(target=self._run, name="Ponte-rede", daemon=True)
        self.thread.start()
        if not self.started.wait(5):
            raise RuntimeError("Não foi possível iniciar a rede do Ponte.")

    def _run(self):
        self.loop = NetworkEventLoop()
        asyncio.set_event_loop(self.loop)
        self.service = TunnelService(self._service_event)
        self.updater = UpdateManager(self.event.emit, lambda:
                                     bool(self.service.bus or self.service.starting or self.service.server))
        self.started.set()
        self.loop.run_forever()
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        self.loop.close()

    def _service_event(self, event):
        self.event.emit(event)
        rtt = event.get("rtt_ms")
        if (event.get("type") == "peer" and isinstance(rtt, (int, float)) and rtt >= 250
                and self.service and self.service.bus and time.perf_counter() - self.last_spike_capture >= 30):
            self.last_spike_capture = time.perf_counter()
            self.spike_capture_index = (self.spike_capture_index + 1) % 3
            snapshot = self.service.diagnostics()
            snapshot["trigger"] = "RTT de aplicativo >= 250 ms; contexto de pico, não diagnóstico conclusivo"
            folder = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Ponte" / "Diagnosticos"
            path = folder / f"Ponte-pico-{self.spike_capture_index}.json"
            async def save_spike():
                try:
                    folder.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(path.write_text, json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
                except OSError:
                    logging.getLogger(__name__).warning("Não foi possível salvar o contexto de um pico de latência")
            asyncio.create_task(save_spike())
        if event.get("type") == "status" and event.get("state") == "stopped" and self.updater:
            self.pending_update_idle = True
            self._schedule_update(self.updater.idle())

    def _schedule_update(self, coroutine):
        if self.update_task and not self.update_task.done():
            coroutine.close()
            return
        self.pending_update_idle = False
        self.update_task = asyncio.create_task(coroutine)
        self.update_task.add_done_callback(self._update_finished)

    def _update_finished(self, task):
        if self.pending_update_idle and not self.updater.installing:
            self._schedule_update(self.updater.idle())

    @Slot()
    def check_updates(self):
        self.loop.call_soon_threadsafe(lambda: self._schedule_update(self.updater.check()))

    @Slot()
    def install_update(self):
        self.loop.call_soon_threadsafe(lambda: self._schedule_update(self.updater.install()))

    def _submit(self, coroutine, operation="connection", **context):
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        def finished(result):
            try:
                result.result()
            except concurrent.futures.CancelledError:
                return
            except Exception as exc:
                logging.getLogger(__name__).warning("Operação falhou: %s", type(exc).__name__)
                fallback = {
                    "trust": "Não consegui atualizar a lista de confiança. Tente novamente.",
                    "room": "Não consegui concluir esta ação na sala. Tente novamente.",
                }.get(operation, "Não consegui concluir a conexão. Confira a internet e tente novamente.")
                message = str(exc) if isinstance(exc, (ValueError, RuntimeError, TimeoutError)) else fallback
                self.event.emit({"type": "error", "message": message, "operation": operation, **context})
        future.add_done_callback(finished)
        return future

    @Slot(int, str)
    def host(self, port, name):
        if self.updater.installing:
            return
        self.event.emit({"type": "status", "state": "connecting", "role": "host", "message": "Abrindo sua sala…"})
        self._submit(self.service.host(port, name))

    @Slot(str, int, str)
    def join(self, code, port=0, name="Amigo"):
        if self.updater.installing:
            return
        self.event.emit({"type": "status", "state": "connecting", "role": "guest", "message": "Encontrando o mundo…"})
        self._submit(self.service.join(code, port, name))

    @Slot(str, bool)
    def approve(self, peer_id, remember=False):
        self._submit(self.service.approve(peer_id, remember), "room", peer_id=peer_id)

    @Slot(str)
    def reject(self, peer_id):
        self._submit(self.service.reject(peer_id), "room", peer_id=peer_id)

    @Slot(str)
    def remove_peer(self, peer_id):
        self._submit(self.service.remove_peer(peer_id), "room", peer_id=peer_id)

    @Slot(str, bool)
    def set_trusted(self, peer_id, trusted):
        self._submit(self.service.set_trusted(peer_id, trusted), "trust", peer_id=peer_id)

    @Slot(str)
    def forget_trusted(self, fingerprint):
        self._submit(self.service.forget_trusted(fingerprint), "trust", fingerprint=fingerprint)

    @Slot()
    def trusted_computers(self):
        self._submit(self.service.trusted_computers(), "trust")

    @Slot()
    def renew_invite(self):
        self._submit(self.service.renew_invite(), "room")

    @Slot()
    def stop(self):
        self._submit(self.service.stop())

    @Slot()
    def scan(self):
        async def scan_worlds():
            self.event.emit({"type": "worlds", "worlds": await discover()})
        self._submit(scan_worlds(), "scan")

    @Slot()
    def diagnostics(self):
        async def save():
            folder = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Ponte" / "Diagnosticos"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / ("Ponte-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
            data = self.service.diagnostics()
            data["system"] = platform.platform()
            data["notice"] = "Sem convite, nome de jogador, endereço IP remoto ou conteúdo do mundo."
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            self.event.emit({"type": "diagnostics", "path": str(path)})
        self._submit(save(), "diagnostics")

    def shutdown(self):
        if not self.loop or self.loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self.service.stop(), self.loop).result(timeout=8)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=3)
