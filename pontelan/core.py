"""TCP streams over authenticated, ordered WebRTC data channels. No game parsing."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import struct
import time
import uuid
from collections import deque
from collections.abc import Callable

from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

from .signaling import SignalBus, new_code, normalize_code
from .latency import LatencySamples, summary
from .flow import DeliveryWindow

LOG = logging.getLogger(__name__)
CHUNK = 16384
SEND_CHUNK = 4096
PEER_SEND_BUDGET = 131072
WINDOW = 262144
MAX_PEERS = 8
MAX_STREAMS = 32
STUN = "stun:stun.cloudflare.com:3478"


def direct_description(sdp: str) -> str:
    if not isinstance(sdp, str) or len(sdp) > 128000:
        raise ValueError("Descrição da conexão inválida.")
    if re.search(r"^a=candidate:.*\btyp\s+relay\b", sdp, re.MULTILINE | re.IGNORECASE):
        raise ValueError("Esta sala exige conexão direta; retransmissão não é aceita.")
    return sdp


def safe_name(name: str) -> str:
    return "".join(c for c in str(name) if c.isprintable())[:60].strip() or "Meu mundo"


class StreamBridge:
    """Preserve bytes and half-close; receive credits bound memory on slow clients."""

    def __init__(self, peer, channel, reader=None, writer=None):
        self.peer, self.channel = peer, channel
        self.reader, self.writer = reader, writer
        self.queue = asyncio.Queue(maxsize=130)
        self.opened = asyncio.Event()
        self.credit_changed = asyncio.Event()
        self.credit = WINDOW
        self.pending_sizes = deque()
        self.pending_times = deque()
        if not hasattr(peer, "delivery"):
            peer.delivery = DeliveryWindow(PEER_SEND_BUDGET, quantum=SEND_CHUNK)
        self.queued_bytes = 0
        self.queued_frames = 0
        self.eof_acked = asyncio.Event()
        self.received_eof = False
        self.sent_eof = False
        self.closed = False
        self.task = None
        channel.bufferedAmountLowThreshold = WINDOW // 2
        channel.on("open", self.opened.set)
        channel.on("message", self._message)
        channel.on("close", self._channel_closed)
        if channel.readyState == "open":
            self.opened.set()

    def _message(self, data):
        if self.closed:
            return
        if not isinstance(data, bytes) or not data:
            self.close()
            return
        if data == b"\x04":
            self.eof_acked.set()
            return
        if data[0] == 3:
            if len(data) != 5:
                self.close()
                return
            count = struct.unpack("!I", data[1:])[0]
            if not self.pending_sizes or count != self.pending_sizes[0] or self.credit + count > WINDOW:
                self.close()
                return
            self.pending_sizes.popleft()
            if self.pending_times:
                sent_at = self.pending_times.popleft()
                self.peer.delivery.release(self, count, time.perf_counter() - sent_at)
            self.credit += count
            self.credit_changed.set()
            return
        if data[0] not in (0, 1, 2) or len(data) > CHUNK + 1:
            self.close()
            return
        if data[0] == 1:
            if len(data) != 1 or self.received_eof:
                self.close()
                return
            self.received_eof = True
        if data[0] == 0:
            self.queued_bytes += len(data) - 1
            self.queued_frames += 1
            if self.queued_bytes > WINDOW or self.queued_frames > 128 or self.received_eof:
                self.close()
                return
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            self.close()

    async def run(self, host_port=None):
        pumps = []
        try:
            await asyncio.wait_for(self.opened.wait(), 20)
            if self.closed:
                return
            if self.reader is None:
                self.reader, self.writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", host_port), 5
                )
            pumps = [asyncio.create_task(self._read()), asyncio.create_task(self._write())]
            await asyncio.gather(*pumps)
            # aiortc's stream reset can overtake messages still in its application
            # queue. Drain them before requesting the reset, including the FIN ack.
            async with asyncio.timeout(10):
                while self.channel.bufferedAmount and self.channel.readyState == "open":
                    await asyncio.sleep(.01)
        except (OSError, asyncio.TimeoutError, ConnectionError):
            if self.channel.readyState == "open":
                with contextlib.suppress(Exception):
                    self.channel.send(b"\x02")
        except asyncio.CancelledError:
            pass
        except Exception:
            LOG.exception("Falha ao encaminhar um fluxo do jogo")
        finally:
            for task in pumps:
                task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            self.close()
            if self.writer:
                with contextlib.suppress(Exception):
                    await self.writer.wait_closed()
            self.peer.streams.discard(self)

    async def _read(self):
        while not self.closed:
            data = await self.reader.read(SEND_CHUNK)
            if not data:
                self.sent_eof = True
                self.channel.send(b"\x01")
                await asyncio.wait_for(self.eof_acked.wait(), 60)
                return
            while self.credit < len(data) or len(self.pending_sizes) >= 128:
                self.credit_changed.clear()
                if self.closed:
                    return
                await self.credit_changed.wait()
            await self.peer.delivery.acquire(self, len(data))
            if self.closed or self.channel.readyState != "open":
                self.peer.delivery.release(self, len(data))
                return
            self.credit -= len(data)
            self.pending_sizes.append(len(data))
            self.pending_times.append(time.perf_counter())
            self.channel.send(b"\x00" + data)
            self.peer.owner.sent += len(data)
            # Give small requests, credit replies and other TCP flows a turn.
            await asyncio.sleep(0)

    async def _write(self):
        while not self.closed:
            frame = await self.queue.get()
            if frame[0] == 2:
                raise ConnectionError("O jogo fechou o fluxo remoto.")
            if frame[0] == 1:
                if self.writer.can_write_eof():
                    self.writer.write_eof()
                    await self.writer.drain()
                if self.channel.readyState == "open":
                    self.channel.send(b"\x04")
                return
            data = frame[1:]
            if not data:
                raise ConnectionError("Fluxo vazio inválido.")
            self.writer.write(data)
            await self.writer.drain()
            self.queued_bytes -= len(data)
            self.queued_frames -= 1
            self.peer.owner.received += len(data)
            if self.channel.readyState == "open":
                self.channel.send(b"\x03" + struct.pack("!I", len(data)))

    def _channel_closed(self):
        # Ordered FIN has already arrived; let queued TCP writes finish before
        # closing their writer. An abrupt reset still aborts promptly.
        if self.received_eof and self.sent_eof:
            self.eof_acked.set()
        else:
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.peer.delivery.forget(self)
        self.opened.set()
        self.credit_changed.set()
        self.eof_acked.set()
        if self.writer:
            self.writer.close()
        if self.channel.readyState != "closed":
            self.channel.close()
        if self.task and self.task is not asyncio.current_task():
            self.task.cancel()


class TunnelPeer:
    def __init__(self, owner, peer_id, name):
        self.owner, self.peer_id, self.name = owner, peer_id, safe_name(name)
        configuration = RTCConfiguration(iceServers=[RTCIceServer(urls=STUN)] if owner.stun else [])
        self.pc = RTCPeerConnection(configuration)
        self.control = None
        self.streams = set()
        self.delivery = DeliveryWindow(PEER_SEND_BUDGET, quantum=SEND_CHUNK)
        self.ready = asyncio.Event()
        self.closed = False
        self.rtt_ms = None
        self.latency = LatencySamples()
        self.ping_task = None
        self.connect_task = None
        self.pc.on("datachannel", self._datachannel)
        self.pc.on("connectionstatechange", self._state)

    def notify(self, state):
        self.rtt_ms = self.latency.latest()
        stats = self.latency.diagnostics()
        self.owner.emit({"type": "peer", "peer_id": self.peer_id, "name": self.name,
                         "state": state, "rtt_ms": self.rtt_ms, "route": self.route(),
                         "rtt_p50_ms": stats["p50_ms"], "rtt_p95_ms": stats["p95_ms"],
                         "rtt_sample_age": stats["sample_age_seconds"]})

    def route(self):
        try:
            connection = self.pc.sctp.transport.transport._connection
            pair = next(iter(connection._nominated.values()))
            return f"{pair.local_candidate.type} ↔ {pair.remote_candidate.type}"
        except (AttributeError, StopIteration):
            return "Direta" if self.ready.is_set() else "Verificando"

    async def _state(self):
        state = self.pc.connectionState
        if state in ("failed", "closed") and not self.closed:
            await self._lost("failed" if state == "failed" else "disconnected")

    async def _lost(self, state="disconnected"):
        if self.closed:
            return
        self.notify(state)
        await self.close()
        if self.owner.role == "guest" and not self.owner.stopping:
            self.owner.emit({"type": "error", "message":
                "A conexão com o anfitrião foi encerrada. Entre novamente na sala."})
            await self.owner.stop()

    def _datachannel(self, channel):
        if channel.label == "control" and self.control is None:
            self._control(channel)
        elif (self.owner.role == "host" and re.fullmatch(r"tcp:[a-f0-9]{32}", channel.label)
              and self.ready.is_set() and len(self.streams) < MAX_STREAMS):
            bridge = StreamBridge(self, channel)
            self.streams.add(bridge)
            bridge.task = asyncio.create_task(bridge.run(self.owner.host_port))
        else:
            channel.close()

    def _control(self, channel):
        self.control = channel
        channel.on("message", self._control_message)
        channel.on("open", self._opened)
        channel.on("close", lambda: asyncio.create_task(self._lost()) if not self.closed else None)
        if channel.readyState == "open":
            self._opened()

    def _opened(self):
        if self.ready.is_set() or self.closed:
            return
        self.ready.set()
        self.notify("connected")
        self.ping_task = asyncio.create_task(self._ping())
        if self.owner.role == "guest":
            self.owner.spawn(self.owner._listen(self))

    def _control_message(self, raw):
        if not isinstance(raw, str) or len(raw) > 1024:
            return
        try:
            data = json.loads(raw)
            if data.get("type") == "ping" and isinstance(data.get("id"), str):
                self.control.send(json.dumps({"type": "pong", "id": data["id"]}))
            elif data.get("type") == "pong":
                value = self.latency.pong(data.get("id"))
                if value is not None:
                    self.rtt_ms = round(value, 2)
                    self.notify("connected")
        except (ValueError, TypeError, ConnectionError):
            return

    async def _ping(self):
        try:
            while not self.closed and self.control.readyState == "open":
                ping_id = uuid.uuid4().hex[:12]
                self.latency.probe(ping_id)
                self.control.send(json.dumps({"type": "ping", "id": ping_id}))
                await asyncio.sleep(3)
        except (asyncio.CancelledError, ConnectionError):
            pass

    async def offer(self):
        self._control(self.pc.createDataChannel("control", ordered=True))
        await asyncio.wait_for(self.pc.setLocalDescription(await self.pc.createOffer()), 18)
        return direct_description(self.pc.localDescription.sdp)

    async def answer(self, sdp):
        await self.pc.setRemoteDescription(RTCSessionDescription(direct_description(sdp), "offer"))
        await asyncio.wait_for(self.pc.setLocalDescription(await self.pc.createAnswer()), 18)
        return direct_description(self.pc.localDescription.sdp)

    async def accept_answer(self, sdp):
        await self.pc.setRemoteDescription(RTCSessionDescription(direct_description(sdp), "answer"))

    async def wait_connected(self):
        try:
            await asyncio.wait_for(self.ready.wait(), 40)
            if self.closed:
                return
        except asyncio.TimeoutError:
            self.notify("failed")
            if self.owner.role == "guest":
                self.owner.emit({"type": "error", "message":
                    "Não foi possível criar o caminho direto. Confira o convite, o mundo aberto e a rede."})
                asyncio.create_task(self.owner.stop())
            await self.close()

    async def accept_tcp(self, reader, writer):
        if self.closed or len(self.streams) >= MAX_STREAMS:
            writer.close()
            return
        channel = self.pc.createDataChannel("tcp:" + uuid.uuid4().hex, ordered=True)
        bridge = StreamBridge(self, channel, reader, writer)
        self.streams.add(bridge)
        bridge.task = asyncio.current_task()
        await bridge.run()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        for stream in list(self.streams):
            stream.close()
        self.delivery.close()
        for task in (self.ping_task, self.connect_task):
            if task and task is not asyncio.current_task():
                task.cancel()
        await self.pc.close()
        if self.owner.peers.get(self.peer_id) is self:
            self.owner.peers.pop(self.peer_id, None)
        self.notify("disconnected")


class TunnelService:
    def __init__(self, emit: Callable[[dict], None], *, endpoint="https://ntfy.sh", stun=True):
        self.emit = emit
        self.endpoint, self.stun = endpoint, stun
        self.bus = None
        self.peers = {}
        self.role = ""
        self.code = ""
        self.name = ""
        self.host_port = 0
        self.local_port = 0
        self.server = None
        self.sent = self.received = 0
        self.stopping = False
        self.metrics_task = None
        self.loop_lag_task = None
        self.loop_lag = deque(maxlen=120)
        self.tasks = set()
        self.generation = 0
        self.starting = False

    async def _measure_loop(self):
        try:
            while True:
                start = time.perf_counter()
                await asyncio.sleep(.5)
                self.loop_lag.append(max(0, (time.perf_counter() - start - .5) * 1000))
        except asyncio.CancelledError:
            pass

    def _begin(self):
        if self.bus or self.starting:
            raise ValueError("Encerre a conexão atual antes de iniciar outra.")
        self.generation += 1
        self.starting = True
        self.stopping = False
        self.loop_lag.clear()
        return self.generation

    def _check_start(self, generation):
        if generation != self.generation or self.stopping:
            raise asyncio.CancelledError()

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def signal_status(self, message):
        state = "hosting" if self.role == "host" else (
            "connected" if any(p.ready.is_set() and not p.closed for p in self.peers.values()) else "connecting")
        self.emit({"type": "status", "state": state,
                   "role": self.role, "message": message})

    async def host(self, port, name="Meu mundo"):
        if self.bus or self.starting:
            raise ValueError("Encerre a sala atual antes de abrir outra.")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("Informe a porta mostrada pelo Minecraft ao abrir para LAN.")
        generation = self._begin()
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 3)
            writer.close()
            await writer.wait_closed()
        except asyncio.CancelledError:
            raise
        except (OSError, asyncio.TimeoutError) as exc:
            if generation == self.generation:
                self.starting = False
            raise ValueError("Não encontrei o jogo nessa porta. Abra o mundo para LAN e confira o número.") from exc
        self._check_start(generation)
        self.role, self.host_port, self.name = "host", port, safe_name(name)
        self.stopping = False
        self.sent = self.received = 0
        self.code = new_code()
        self.signal_status("Preparando o convite…")
        bus = SignalBus(self.code, "host", True, self._signal,
                        self.signal_status, endpoint=self.endpoint)
        self.bus = bus
        try:
            await bus.start()
            self._check_start(generation)
        except BaseException:
            if generation == self.generation:
                await self.stop()
            else:
                await bus.close()
            raise
        self.starting = False
        self.emit({"type": "room", "code": self.code, "name": self.name, "port": port})
        self.emit({"type": "status", "state": "hosting", "role": "host", "message": "Sala aberta. Convide seus amigos."})
        self.metrics_task = asyncio.create_task(self._metrics())
        self.loop_lag_task = asyncio.create_task(self._measure_loop())
        return self.code

    async def join(self, code, port=0, name="Amigo"):
        if self.bus or self.starting:
            raise ValueError("Encerre a conexão atual antes de entrar em outra sala.")
        normalized = normalize_code(code)
        if not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("Porta local inválida.")
        generation = self._begin()
        self.code, self.role, self.local_port = normalized, "guest", port
        self.stopping = False
        self.sent = self.received = 0
        self.signal_status("Encontrando o mundo…")
        bus = SignalBus(self.code, uuid.uuid4().hex, False, self._signal,
                        self.signal_status, endpoint=self.endpoint)
        self.bus = bus
        try:
            await bus.start()
            self._check_start(generation)
            peer = TunnelPeer(self, "host", "Anfitrião")
            self.peers["host"] = peer
            peer.notify("connecting")
            sdp = await peer.offer()
            self._check_start(generation)
            await bus.send("host", {"type": "offer", "sdp": sdp, "name": safe_name(name)})
            self._check_start(generation)
            peer.connect_task = asyncio.create_task(peer.wait_connected())
            self.metrics_task = asyncio.create_task(self._metrics())
            self.loop_lag_task = asyncio.create_task(self._measure_loop())
            self.starting = False
        except BaseException:
            if generation == self.generation:
                await self.stop()
            else:
                await bus.close()
            raise

    async def _signal(self, message):
        # Never block the subscription reader on ICE gathering or TCP work.
        if not self.stopping:
            self.spawn(self._handle_signal(message))

    async def _handle_signal(self, message):
        sender, kind = message.get("_sender"), message.get("type")
        try:
            if self.role == "host" and kind == "offer" and sender != "host":
                if sender in self.peers:
                    return
                if len(self.peers) >= MAX_PEERS:
                    await self.bus.send(sender, {"type": "error", "message": "A sala já tem oito amigos conectados."})
                    return
                peer = TunnelPeer(self, sender, message.get("name", "Amigo"))
                self.peers[sender] = peer
                peer.notify("connecting")
                sdp = await peer.answer(message.get("sdp"))
                if self.stopping or not self.bus:
                    await peer.close()
                    return
                await self.bus.send(sender, {"type": "answer", "sdp": sdp, "name": self.name})
                peer.connect_task = asyncio.create_task(peer.wait_connected())
            elif self.role == "guest" and sender == "host" and kind == "answer":
                peer = self.peers.get("host")
                if peer and peer.pc.signalingState == "have-local-offer":
                    peer.name = safe_name(message.get("name", "Anfitrião"))
                    await peer.accept_answer(message.get("sdp"))
            elif kind == "bye" and sender in self.peers:
                await self.peers[sender].close()
                if self.role == "guest":
                    self.emit({"type": "error", "message": "O anfitrião encerrou a sala."})
                    asyncio.create_task(self.stop())
            elif self.role == "guest" and sender == "host" and kind == "error":
                self.emit({"type": "error", "message": safe_name(message.get("message", "Não foi possível entrar na sala."))})
                asyncio.create_task(self.stop())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("Negociação interrompida: %s", type(exc).__name__)
            if sender in self.peers:
                await self.peers[sender].close()
            self.emit({"type": "error", "message": "Não foi possível negociar a conexão direta. Tente entrar novamente.",
                       "operation": "peer" if self.role == "host" else "connection"})
            if self.role == "guest":
                asyncio.create_task(self.stop())

    async def _listen(self, peer):
        if self.stopping or peer.closed:
            return
        try:
            server = await asyncio.start_server(peer.accept_tcp, "127.0.0.1", self.local_port)
            if self.stopping or peer.closed:
                server.close()
                await server.wait_closed()
                return
            self.server = server
            self.local_port = self.server.sockets[0].getsockname()[1]
            self.emit({"type": "listening", "address": f"127.0.0.1:{self.local_port}"})
            self.emit({"type": "status", "state": "connected", "role": "guest",
                       "message": "Conexão direta pronta. Entre pelo Minecraft."})
        except OSError:
            self.emit({"type": "error", "message": "A porta local está ocupada. Encerre a conexão e tente novamente."})
            asyncio.create_task(self.stop())

    async def _metrics(self):
        previous_sent, previous_received, previous_time = self.sent, self.received, time.perf_counter()
        try:
            while True:
                now = time.perf_counter()
                elapsed = max(.001, now - previous_time)
                rtts = []
                for peer in list(self.peers.values()):
                    latest = peer.latency.latest()
                    if latest is not None:
                        rtts.append(latest)
                    if latest != peer.rtt_ms and peer.ready.is_set() and not peer.closed:
                        peer.notify("connected")
                self.emit({"type": "metrics", "sent": self.sent, "received": self.received,
                           "sent_bytes_sec": round((self.sent - previous_sent) / elapsed),
                           "received_bytes_sec": round((self.received - previous_received) / elapsed),
                           "rtt_ms": round(max(rtts), 2) if rtts else None})
                previous_sent, previous_received, previous_time = self.sent, self.received, now
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            pass

    async def stop(self):
        if self.stopping:
            return
        self.generation += 1
        self.starting = False
        self.stopping = True
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for task in list(self.tasks):
            if task is not asyncio.current_task():
                task.cancel()
        await asyncio.gather(*(t for t in list(self.tasks) if t is not asyncio.current_task()), return_exceptions=True)
        # Data-channel closure is immediate. A bye publish is best-effort only.
        for peer in list(self.peers.values()):
            await peer.close()
        self.peers.clear()
        if self.metrics_task:
            self.metrics_task.cancel()
            await asyncio.gather(self.metrics_task, return_exceptions=True)
            self.metrics_task = None
        if self.loop_lag_task:
            self.loop_lag_task.cancel()
            await asyncio.gather(self.loop_lag_task, return_exceptions=True)
            self.loop_lag_task = None
        if self.bus:
            bus, self.bus = self.bus, None
            await bus.close()
        self.code = ""
        self.emit({"type": "status", "state": "stopped", "message": "Conexão encerrada.", "role": self.role})

    def diagnostics(self):
        from . import __version__
        return {"version": __version__, "role": self.role, "active": bool(self.bus),
                "sent": self.sent, "received": self.received,
                "event_loop_lag": summary(self.loop_lag),
                "peers": [{"state": p.pc.connectionState, "route": p.route(), "rtt_ms": p.latency.latest(),
                           "latency": p.latency.diagnostics(), "streams": len(p.streams),
                           "delivery": p.delivery.diagnostics(),
                           "buffered_bytes": sum(s.channel.bufferedAmount for s in p.streams),
                           "receive_queued_bytes": sum(s.queued_bytes for s in p.streams),
                           "send_unacked_bytes": sum(WINDOW - s.credit for s in p.streams)} for p in self.peers.values()],
                "transport": "WebRTC / ICE / STUN; sem TURN", "signaling": self.endpoint}
