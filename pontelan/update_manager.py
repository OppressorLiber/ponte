"""Background update state, independent of the connection lifecycle."""
import asyncio
import contextlib
import sys

from . import __version__
from .updates import discover_update, download_update
from .update_helper import (load_config, update_directory, verify_package,
                            prepare_update, launch_helper, terminate_child_tree)


class PauseDownload(Exception):
    pass


class UpdateManager:
    def __init__(self, emit, is_busy):
        self.emit = emit
        self.is_busy = is_busy
        self.info = None
        self.package = None
        self.working = False
        self.installing = False
        self.task = None
        self.job_path = None

    def event(self, state, **fields):
        self.emit({"type": "update", "state": state,
                   "version": self.info.version if self.info else __version__, **fields})

    async def check(self):
        if self.working or self.installing:
            return
        self.working = True
        try:
            if self.package and self.info:
                try:
                    await asyncio.to_thread(verify_package, self.package, self.info)
                    self.event("ready")
                    return
                except (RuntimeError, OSError):
                    self.package = None
            self.event("checking")
            self.info = await discover_update(__version__, load_config())
            if self.info is None:
                self.event("current")
                return
            self.event("available", notes=self.info.notes)
            if self.is_busy():
                self.event("available", message="Nova versão disponível. O download começa ao encerrar a sessão.")
                return
            await self._download()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.event("error", message=str(exc) if isinstance(exc, RuntimeError) else
                       "Não foi possível verificar as atualizações. Tente novamente mais tarde.")
        finally:
            self.working = False

    async def _download(self):
        if not self.info:
            return
        destination = update_directory() / ("Ponte-" + self.info.version + ".exe")
        if destination.exists():
            with contextlib.suppress(RuntimeError, OSError):
                await asyncio.to_thread(verify_package, destination, self.info)
                self.package = destination
                self.event("ready")
                return
        previous = -1
        def progress(done, total):
            nonlocal previous
            if self.is_busy():
                raise PauseDownload()
            percent = min(100, int(done * 100 / total))
            if percent != previous:
                previous = percent
                self.event("downloading", progress=percent)
        try:
            self.event("downloading", progress=0)
            self.package = await download_update(self.info, destination, progress)
            self.event("ready")
        except PauseDownload:
            self.event("available", message="Download pausado enquanto a sessão estiver aberta.")

    async def idle(self):
        if self.package and self.info:
            self.event("ready")
        elif self.info and not self.working and not self.installing and not self.is_busy():
            self.working = True
            try:
                await self._download()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.event("error", message=str(exc) if isinstance(exc, RuntimeError) else
                           "Não foi possível baixar a atualização. Tente novamente.")
            finally:
                self.working = False

    async def install(self):
        if self.installing:
            return
        if self.is_busy():
            self.event("ready", message="Encerre a sessão antes de reiniciar e atualizar.")
            return
        if not self.package or not self.info:
            await self.check()
            return
        if not getattr(sys, "frozen", False):
            self.event("error", message="Use o executável portátil para instalar automaticamente.")
            return
        self.installing = True
        process = None
        try:
            job, helper = await asyncio.to_thread(prepare_update, self.info, self.package)
            self.job_path = job
            if self.is_busy():
                self.installing = False
                self.event("ready", message="A sessão começou. A atualização continua pronta para depois.")
                return
            process = launch_helper(job, helper)
            import json
            job_data = json.loads(job.read_text(encoding="utf-8"))
            ready_file = job.with_name("helper-ready-" + job_data["token"] + ".json")
            async with asyncio.timeout(25):
                while not ready_file.exists():
                    if process.poll() is not None:
                        raise RuntimeError("O atualizador não iniciou. O aplicativo atual continua aberto.")
                    await asyncio.sleep(.1)
            # A session could have been opened while the helper was unpacking.
            # Do not quit until the caller is still idle.
            if self.is_busy():
                await asyncio.to_thread(terminate_child_tree, process)
                self.installing = False
                self.event("ready", message="Encerre a sessão antes de instalar a atualização.")
                return
            self.event("installing")
        except asyncio.CancelledError:
            if process is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(terminate_child_tree, process)
            self.installing = False
            raise
        except Exception as exc:
            if process is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(terminate_child_tree, process)
            self.installing = False
            self.event("error", message=str(exc) if isinstance(exc, RuntimeError) else
                       "Não foi possível instalar. O aplicativo atual continua disponível.")
