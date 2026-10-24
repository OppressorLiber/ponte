"""Separate-process Windows updater. It never touches settings or game files."""
from __future__ import annotations

import base64
import contextlib
import ctypes
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from .updates import verify_manifest


def load_config():
    return json.loads(Path(__file__).with_name("update_config.json").read_text(encoding="utf-8"))


def update_directory():
    path = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Ponte" / "updates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def file_hash(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def restart_environment():
    env = os.environ.copy()
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    # An external helper/restart must not inherit temporary library locations.
    for name in ("OPENSSL_MODULES", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        if name in env and getattr(sys, "_MEIPASS", "_no_meipass_") in env[name]:
            env.pop(name)
    return env


def verify_package(path, info):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or path.stat().st_size != info.size or file_hash(path) != info.sha256:
        raise RuntimeError("O arquivo da atualização não passou na verificação. Baixe novamente.")


def prepare_update(info, package, *, executable=None, parent_pid=None):
    if executable is None and not getattr(sys, "frozen", False):
        raise RuntimeError("A atualização automática funciona no aplicativo portátil para Windows.")
    target = Path(executable or sys.executable).absolute()
    if target.is_symlink() or target.suffix.lower() != ".exe" or not target.is_file():
        raise RuntimeError("O local deste aplicativo não permite a atualização automática.")
    verify_manifest(info.manifest_bytes, info.signature, load_config(), expected_version=info.version)
    verify_package(package, info)
    # Fail before closing the working application if its directory is read-only.
    try:
        with tempfile.NamedTemporaryFile(prefix=".ponte-write-", dir=target.parent, delete=True):
            pass
    except OSError as exc:
        raise RuntimeError("Mova o Ponte para uma pasta em que você pode gravar arquivos e tente novamente.") from exc
    directory = update_directory()
    identifier = uuid.uuid4().hex
    helper = directory / ("helper-" + identifier + ".exe")
    job_path = directory / ("job-" + identifier + ".json")
    job = {"schema": 1, "target": str(target), "package": str(Path(package).absolute()),
           "parent_pid": parent_pid or os.getpid(), "old_sha256": file_hash(target),
           "manifest": base64.b64encode(info.manifest_bytes).decode(),
           "signature": base64.b64encode(info.signature).decode(),
           "token": uuid.uuid4().hex, "helper": str(helper),
           "ack": str(directory / ("ack-" + identifier + ".json")),
           "result": str(directory / ("result-" + identifier + ".json"))}
    shutil.copy2(target, helper)
    job_path.write_text(json.dumps(job), encoding="utf-8")
    return job_path, helper


def launch_helper(job_path, helper):
    # Windows disallows CREATE_NO_WINDOW with DETACHED_PROCESS; detached avoids
    # inheriting the app's process lifetime and does not create a GUI console.
    flags = getattr(subprocess, "DETACHED_PROCESS", 0)
    return subprocess.Popen([str(helper), "--apply-update", str(job_path)],
                            cwd=str(Path(helper).parent), env=restart_environment(),
                            creationflags=flags, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            close_fds=True)


def wait_parent(pid, timeout=90):
    if sys.platform != "win32":
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x00100000, False, int(pid))
    if not handle:
        if ctypes.get_last_error() == 87:  # Process already exited.
            return
        raise RuntimeError("Não foi possível aguardar o aplicativo fechar.")
    try:
        if kernel.WaitForSingleObject(handle, int(timeout * 1000)) != 0:
            raise RuntimeError("O aplicativo ainda está em uso. Tente atualizar depois de fechá-lo.")
    finally:
        kernel.CloseHandle(handle)


def replace_retry(source, destination, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.2)


@contextlib.contextmanager
def acquire_install_lock(path):
    """Kernel-owned lock is released even if the updater process crashes."""
    if sys.platform == "win32":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.CreateMutexW(None, True, "Local\\PonteUpdate-" + Path(path).stem)
        if not handle:
            raise RuntimeError("Não foi possível reservar a atualização.")
        if ctypes.get_last_error() == 183:
            kernel.CloseHandle(handle)
            raise RuntimeError("Uma atualização deste aplicativo já está em andamento.")
        try:
            yield
        finally:
            kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)
    else:
        import fcntl
        with Path(path).open("a+b") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Uma atualização já está em andamento.") from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def start_application(target, job_path=None, *, error=False):
    args = [str(target)]
    if job_path:
        args += ["--update-error" if error else "--update-ack", str(job_path)]
    return subprocess.Popen(args, cwd=str(Path(target).parent), env=restart_environment(),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True)


def terminate_child_tree(process):
    """Stop only a helper/application process that this updater just started."""
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run([str(Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32" / "taskkill.exe"),
                        "/PID", str(process.pid), "/T", "/F"],
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    else:
        process.terminate()
    with contextlib.suppress(Exception):
        process.wait(timeout=5)


def confirm_ready(job_path, executable=None):
    """Called only after the new GUI and network controller have initialized."""
    path = Path(job_path).absolute()
    if path.parent != update_directory().absolute() or path.stat().st_size > 128000:
        raise RuntimeError("Confirmação de atualização inválida.")
    job = json.loads(path.read_text(encoding="utf-8"))
    info = verify_manifest(base64.b64decode(job["manifest"], validate=True),
                           base64.b64decode(job["signature"], validate=True), load_config())
    from . import __version__
    target = Path(executable or sys.executable).absolute()
    if str(target) != job["target"] or __version__ != info.version or file_hash(target) != info.sha256:
        raise RuntimeError("A versão iniciada não corresponde à atualização.")
    ack = Path(job["ack"])
    if ack.parent != path.parent or not ack.name.startswith("ack-"):
        raise RuntimeError("Confirmação de atualização inválida.")
    ack.write_text(json.dumps({"token": job["token"], "version": __version__,
                               "sha256": info.sha256, "pid": os.getpid()}), encoding="utf-8")


def apply_update(job_path, *, config=None, helper_path=None, wait=wait_parent,
                 start=start_application, ready_timeout=45):
    path = Path(job_path).absolute()
    directory = update_directory().absolute()
    if path.parent != directory or path.stat().st_size > 128000:
        raise RuntimeError("Pedido de atualização inválido.")
    job = json.loads(path.read_text(encoding="utf-8"))
    info = verify_manifest(base64.b64decode(job["manifest"], validate=True),
                           base64.b64decode(job["signature"], validate=True), config or load_config())
    target, package = Path(job["target"]), Path(job["package"])
    helper = Path(helper_path or sys.executable)
    if (not target.is_absolute() or target.suffix.lower() != ".exe" or target.is_symlink()
            or package.parent != directory or package.is_symlink()
            or not target.is_file() or file_hash(target) != job["old_sha256"]
            or file_hash(helper) != job["old_sha256"]):
        raise RuntimeError("O aplicativo original mudou. Abra-o e tente atualizar novamente.")
    ack, result = Path(job["ack"]), Path(job["result"])
    if (ack.parent != directory or result.parent != directory
            or not isinstance(job["token"], str) or not re.fullmatch(r"[a-f0-9]{32}", job["token"])):
        raise RuntimeError("Pedido de atualização inválido.")
    verify_package(package, info)
    lock = directory / ("install-" + hashlib.sha256(str(target).casefold().encode()).hexdigest()[:20] + ".lock")
    lock_context = acquire_install_lock(lock)
    lock_context.__enter__()
    backup = target.with_name(".ponte-backup-" + job["token"] + ".exe")
    stage = target.with_name(".ponte-stage-" + job["token"] + ".exe")
    new_process = None
    replaced = False
    parent_exited = False
    try:
        path.with_name("helper-ready-" + job["token"] + ".json").write_text(
            json.dumps({"token": job["token"], "pid": os.getpid()}), encoding="utf-8")
        wait(int(job["parent_pid"]))
        parent_exited = True
        if file_hash(target) != job["old_sha256"]:
            raise RuntimeError("O aplicativo original mudou durante a atualização.")
        shutil.copy2(package, stage)
        verify_package(stage, info)
        ack.unlink(missing_ok=True)
        replace_retry(target, backup)
        try:
            replace_retry(stage, target)
            replaced = True
        except BaseException:
            replace_retry(backup, target)
            raise
        new_process = start(target, path)
        deadline = time.monotonic() + ready_timeout
        ready = False
        while time.monotonic() < deadline:
            if ack.exists():
                with contextlib.suppress(ValueError, OSError):
                    confirmation = json.loads(ack.read_text(encoding="utf-8"))
                    if (isinstance(confirmation, dict) and confirmation.get("token") == job["token"] and confirmation.get("version") == info.version
                            and confirmation.get("sha256") == info.sha256):
                        ready = True
                        break
            if new_process.poll() is not None:
                break
            time.sleep(.2)
        if not ready:
            raise RuntimeError("A nova versão não confirmou a inicialização. A versão anterior será restaurada.")
        # Keep one working backup in our cache, not alongside the user's app.
        with contextlib.suppress(OSError):
            shutil.copy2(backup, directory / "Ponte-previous.exe")
            backup.unlink()
        with contextlib.suppress(OSError):
            result.write_text(json.dumps({"updated": True, "version": info.version,
                                          "sha256": file_hash(target)}), encoding="utf-8")
        return True
    except BaseException as exc:
        if replaced and backup.exists():
            if new_process and new_process.poll() is None:
                terminate_child_tree(new_process)
            replace_retry(backup, target)
        if parent_exited and target.exists():
            with contextlib.suppress(Exception):
                start(target, path, error=True)
        with contextlib.suppress(OSError):
            result.write_text(json.dumps({"updated": False, "message": str(exc)[:500]}), encoding="utf-8")
        return False
    finally:
        with contextlib.suppress(OSError):
            stage.unlink(missing_ok=True)
        lock_context.__exit__(None, None, None)


def main(job_path):
    try:
        return 0 if apply_update(job_path) else 1
    except Exception:
        return 1
