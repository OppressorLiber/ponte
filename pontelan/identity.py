"""Persistent computer keys and an explicit, local list of trusted public keys."""

from __future__ import annotations

import base64
import contextlib
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .signaling import normalize_code

MAX_SDP_BYTES = 128 * 1024
MAX_NAME_BYTES = 256
MAX_TRUSTED_COMPUTERS = 256
_MAX_STORE_BYTES = 128 * 1024
_LOCK_TIMEOUT = 2.0
_TRUST_BUSY = "A lista de computadores confiáveis está em uso. Tente novamente."
_PEER = re.compile(r"[0-9a-f]{32}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_BASE64 = re.compile(r"[A-Za-z0-9_-]+\Z")

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def _data_directory() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Ponte"


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: object, size: int) -> bytes:
    if not isinstance(value, str) or len(value) != (size * 8 + 5) // 6 or not _BASE64.fullmatch(value):
        raise ValueError("Identidade do computador inválida.")
    raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if len(raw) != size or _encode(raw) != value:
        raise ValueError("Identidade do computador inválida.")
    return raw


def _fingerprint(public_key: bytes) -> str:
    return hashlib.sha256(public_key).hexdigest()


def _unique_object(pairs: list[tuple]) -> dict:
    document = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("Campo duplicado na lista de computadores confiáveis.")
        document[key] = value
    return document


def _name_bytes(name: object) -> bytes:
    if not isinstance(name, str) or len(name) > MAX_NAME_BYTES:
        raise ValueError("Nome do computador inválido.")
    try:
        raw = name.encode("utf-8")
    except UnicodeError:
        raise ValueError("Nome do computador inválido.") from None
    if len(raw) > MAX_NAME_BYTES:
        raise ValueError("Nome do computador excedeu o tamanho permitido.")
    return raw


def _offer_payload(code: str, peer_id: str, sdp: str, name: str) -> bytes:
    try:
        code = normalize_code(code)
    except RuntimeError:
        raise ValueError("Convite da identidade inválido.") from None
    if not isinstance(peer_id, str) or not _PEER.fullmatch(peer_id):
        raise ValueError("Conexão da identidade inválida.")
    _name_bytes(name)
    if not isinstance(sdp, str) or not sdp or len(sdp) > MAX_SDP_BYTES:
        raise ValueError("Negociação da identidade inválida.")
    try:
        sdp_bytes = sdp.encode("utf-8")
    except UnicodeError:
        raise ValueError("Negociação da identidade inválida.") from None
    if len(sdp_bytes) > MAX_SDP_BYTES:
        raise ValueError("Negociação da identidade excedeu o tamanho permitido.")
    return json.dumps({
        "context": "ponte-computer-offer-v1", "code": code, "peer_id": peer_id,
        "name": name, "sdp_sha256": hashlib.sha256(sdp_bytes).hexdigest(),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _write_temporary(path: Path, data: bytes) -> Path:
    """Write a complete file before making it visible at its final name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + "-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        return temporary
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


@contextlib.contextmanager
def _trust_file_lock(path: Path, timeout: float | None = None):
    """Serialize mutations using a persistent inode, separate from replaced JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    # Never unlink this sidecar: a replacement would let writers lock different files.
    with lock_path.open("a+b") as lock_file:
        deadline = time.monotonic() + (_LOCK_TIMEOUT if timeout is None else timeout)
        while True:
            try:
                if os.name == "nt":
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(_TRUST_BUSY) from None
                time.sleep(0.025)
        try:
            yield
        finally:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class Identity:
    """A computer identity; only its public proof is sent to another computer."""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key
        raw = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self._public_key = _encode(raw)
        self._fingerprint = _fingerprint(raw)

    @property
    def public_key(self) -> str:
        return self._public_key

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    @classmethod
    def generate(cls) -> Identity:
        """Create an ephemeral identity, useful for temporary peers and tests."""
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def load_or_create(cls, path: str | os.PathLike | None = None) -> Identity:
        path = Path(path) if path is not None else _data_directory() / "identity" / "ed25519-private.pem"
        try:
            return cls._read(path)
        except FileNotFoundError:
            pass
        identity = cls.generate()
        pem = identity._private_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        )
        temporary = _write_temporary(path, pem)
        try:
            try:
                # Linking publishes a complete key without replacing a concurrent winner.
                os.link(temporary, path)
            except FileExistsError:
                return cls._read(path)
            return identity
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink()

    @classmethod
    def _read(cls, path: Path) -> Identity:
        with path.open("rb") as source:
            pem = source.read(4097)
        if len(pem) > 4096:
            raise ValueError("Arquivo de identidade do computador inválido.")
        try:
            key = serialization.load_pem_private_key(pem, password=None)
        except (ValueError, TypeError):
            raise ValueError("Arquivo de identidade do computador inválido.") from None
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("Arquivo de identidade do computador inválido.")
        return cls(key)

    def sign_offer(self, code: str, peer_id: str, sdp: str, name: str) -> dict:
        signature = self._private_key.sign(_offer_payload(code, peer_id, sdp, name))
        return {"public_key": self.public_key, "signature": _encode(signature)}


def verify_offer(code: str, peer_id: str, sdp: str, name: str, proof: object) -> dict:
    """Authenticate the exact offer, independently of the shared invitation key."""
    if not isinstance(proof, dict) or set(proof) != {"public_key", "signature"}:
        raise ValueError("Comprovação da identidade do computador inválida.")
    public_key = _decode(proof["public_key"], 32)
    signature = _decode(proof["signature"], 64)
    payload = _offer_payload(code, peer_id, sdp, name)
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, payload)
    except InvalidSignature:
        raise ValueError("A identidade do computador não corresponde à negociação.") from None
    return {"public_key": proof["public_key"], "fingerprint": _fingerprint(public_key)}


class TrustStore:
    """An allowlist keyed by full public-key fingerprints, never by display name."""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path) if path is not None else _data_directory() / "trusted-computers.json"
        self._memory: dict | None = None
        self._lock = threading.RLock()

    @classmethod
    def in_memory(cls) -> TrustStore:
        store = cls()
        store._memory = {}
        return store

    @contextlib.contextmanager
    def _transaction(self, timeout: float | None = None):
        if not self._lock.acquire(blocking=timeout != 0):
            raise TimeoutError(_TRUST_BUSY)
        try:
            if self._memory is not None:
                yield
            else:
                with _trust_file_lock(self.path, timeout=timeout):
                    yield
        finally:
            self._lock.release()

    def _read(self) -> dict:
        if self._memory is not None:
            return self._memory.copy()
        try:
            with self.path.open("rb") as source:
                raw = source.read(_MAX_STORE_BYTES + 1)
        except FileNotFoundError:
            return {}
        try:
            if len(raw) > _MAX_STORE_BYTES:
                raise ValueError
            document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
            if not isinstance(document, dict) or set(document) != {"version", "computers"} \
                    or type(document["version"]) is not int or document["version"] != 1 \
                    or not isinstance(document["computers"], list) \
                    or len(document["computers"]) > MAX_TRUSTED_COMPUTERS:
                raise ValueError
            records = {}
            for record in document["computers"]:
                if not isinstance(record, dict) or set(record) != {"public_key", "fingerprint", "name"}:
                    raise ValueError
                raw_key = _decode(record["public_key"], 32)
                fingerprint = _fingerprint(raw_key)
                if record["fingerprint"] != fingerprint or fingerprint in records:
                    raise ValueError
                _name_bytes(record["name"])
                records[fingerprint] = record
            return records
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ValueError("Lista de computadores confiáveis inválida; o arquivo foi preservado.") from None

    def _write(self, records: dict) -> None:
        if self._memory is not None:
            self._memory = records
            return
        document = {"version": 1, "computers": list(records.values())}
        raw = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8")
        if len(raw) > _MAX_STORE_BYTES:
            raise ValueError("Lista de computadores confiáveis excedeu o tamanho permitido.")
        temporary = _write_temporary(self.path, raw)
        try:
            os.replace(temporary, self.path)
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink()

    def list(self) -> list[dict]:
        with self._lock:
            return [{"fingerprint": fingerprint, "name": record["name"]}
                    for fingerprint, record in self._read().items()]

    def is_trusted(self, public_key: str) -> bool:
        raw = _decode(public_key, 32)
        with self._lock:
            return _fingerprint(raw) in self._read()

    def remember(self, public_key: str, name: str, *, timeout: float | None = None) -> None:
        raw = _decode(public_key, 32)
        _name_bytes(name)
        fingerprint = _fingerprint(raw)
        with self._transaction(timeout=timeout):
            records = self._read()
            if fingerprint not in records and len(records) >= MAX_TRUSTED_COMPUTERS:
                raise ValueError("Limite de computadores confiáveis atingido.")
            records[fingerprint] = {"public_key": public_key, "fingerprint": fingerprint, "name": name}
            self._write(records)

    def forget(self, fingerprint: str, *, timeout: float | None = None) -> None:
        if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
            raise ValueError("Identificação do computador confiável inválida.")
        with self._transaction(timeout=timeout):
            records = self._read()
            if records.pop(fingerprint, None) is not None:
                self._write(records)
