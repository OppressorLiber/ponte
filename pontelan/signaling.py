"""Encrypted, bounded rendezvous over ntfy; never a transport for game packets."""

from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from collections.abc import Awaitable, Callable
import contextlib
import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.parse import urlsplit
import uuid
import zlib

import aiohttp
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAX_WIRE_BYTES = 4096
MAX_JSON_BYTES = 128 * 1024
MAX_REPLAYS = 1000
MAX_CLOCK_SKEW = 300
_CONTEXT = b"ponte-signal-v1"
_CODE = re.compile(r"PONTE-([A-Za-z0-9_-]{43})\Z")
_PEER = re.compile(r"[0-9a-f]{32}\Z")
_MESSAGE_ID = re.compile(r"[A-Za-z0-9]{1,64}\Z")
_TYPES = frozenset({"hello", "offer", "answer", "bye", "error"})


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def new_code() -> str:
    """Return a portable invitation with an independent 256-bit secret."""
    return "PONTE-" + _b64encode(secrets.token_bytes(32))


def normalize_code(code: str) -> str:
    candidate = code.strip() if isinstance(code, str) else ""
    match = _CODE.fullmatch(candidate)
    if not match:
        raise RuntimeError("Código inválido. Cole o convite completo que começa com PONTE-.")
    secret = _b64decode(match[1])
    if len(secret) != 32 or _b64encode(secret) != match[1]:
        raise RuntimeError("Código inválido. Copie o convite novamente.")
    return candidate


def _valid_peer(peer_id: object) -> bool:
    return isinstance(peer_id, str) and (peer_id == "host" or _PEER.fullmatch(peer_id) is not None)


class SignalBus:
    """One private subscription per peer, with authenticated encrypted messages.

    A shared invite is the trust boundary: its holders can join and authenticate
    signaling. ntfy sees opaque topic names and ciphertext, plus HTTP metadata.
    """

    def __init__(
        self,
        code: str,
        peer_id: str,
        is_host: bool,
        on_message: Callable[[dict], Awaitable[None]],
        on_status: Callable[[str], None] | None = None,
        endpoint: str = "https://ntfy.sh",
    ) -> None:
        self.code = normalize_code(code)
        if not _valid_peer(peer_id) or (is_host and peer_id != "host") or (not is_host and peer_id == "host"):
            raise RuntimeError("Identificação de conexão inválida.")
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"})) \
                or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RuntimeError("Endereço do serviço de sinalização inválido. Use HTTPS.")
        self.peer_id = peer_id
        self.is_host = is_host
        self.endpoint = endpoint.rstrip("/")
        self.on_message = on_message
        self.on_status = on_status
        self._secret = _b64decode(self.code[6:])
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_CONTEXT).derive(self._secret)
        self._cipher = ChaCha20Poly1305(key)
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._closed = False
        self._start_error: RuntimeError | None = None
        self._cursor = str(int(time.time()) - 1)

    def _topic(self, target: str) -> str:
        if not _valid_peer(target):
            raise RuntimeError("Destino de sinalização inválido.")
        digest = hmac.new(self._secret, b"ponte-topic-v1:" + target.encode("ascii"), hashlib.sha256).digest()
        return "ponte_" + _b64encode(digest)  # 49 characters; ntfy allows at most 64.

    def _status(self, message: str) -> None:
        if self.on_status:
            with contextlib.suppress(Exception):
                self.on_status(message)

    def _encode(self, target: str, message: dict) -> str:
        if not _valid_peer(target) or (self.is_host and target == "host") or (not self.is_host and target != "host"):
            raise RuntimeError("Destino de sinalização inválido.")
        if not isinstance(message, dict) or not isinstance(message.get("type"), str) or message["type"] not in _TYPES:
            raise RuntimeError("Esta mensagem não pertence à negociação da conexão.")
        envelope = {
            "v": 1, "time": int(time.time()), "id": uuid.uuid4().hex,
            "sender": self.peer_id, "target": target, "message": message,
        }
        try:
            plain = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise RuntimeError("Mensagem de sinalização inválida.") from None
        if len(plain) > MAX_JSON_BYTES:
            raise RuntimeError("A negociação da conexão excedeu o tamanho permitido.")
        nonce = secrets.token_bytes(12)
        ciphertext = self._cipher.encrypt(nonce, zlib.compress(plain, level=9), _CONTEXT + target.encode("ascii"))
        wire = "p1:" + _b64encode(nonce + ciphertext)
        if len(wire) > MAX_WIRE_BYTES:
            raise RuntimeError("A negociação ficou grande demais para o serviço. Tente novamente com menos interfaces de rede ativas.")
        return wire

    def _decode(self, wire: object) -> dict | None:
        if not isinstance(wire, str) or not 40 <= len(wire) <= MAX_WIRE_BYTES or not wire.startswith("p1:"):
            return None
        try:
            packed = _b64decode(wire[3:])
            if len(packed) < 28 or "p1:" + _b64encode(packed) != wire:
                return None
            compressed = self._cipher.decrypt(packed[:12], packed[12:], _CONTEXT + self.peer_id.encode("ascii"))
            inflater = zlib.decompressobj()
            plain = inflater.decompress(compressed, MAX_JSON_BYTES + 1)
            if len(plain) > MAX_JSON_BYTES or inflater.unconsumed_tail or inflater.unused_data or not inflater.eof:
                return None
            envelope = json.loads(plain)
            if not isinstance(envelope, dict) or type(envelope.get("v")) is not int or envelope["v"] != 1:
                return None
            stamp = envelope.get("time")
            message_id = envelope.get("id")
            sender = envelope.get("sender")
            message = envelope.get("message")
            now = time.time()
            if type(stamp) is not int or abs(now - stamp) > MAX_CLOCK_SKEW \
                    or not isinstance(message_id, str) or not _PEER.fullmatch(message_id) \
                    or not _valid_peer(sender) or sender == self.peer_id \
                    or (self.is_host and sender == "host") or (not self.is_host and sender != "host") \
                    or envelope.get("target") != self.peer_id \
                    or not isinstance(message, dict) or message.get("type") not in _TYPES:
                return None
            for old_id, received_at in list(self._seen.items()):
                if received_at < now - MAX_CLOCK_SKEW:
                    self._seen.pop(old_id, None)
            if message_id in self._seen:
                return None
            self._seen[message_id] = now
            while len(self._seen) > MAX_REPLAYS:
                self._seen.popitem(last=False)
            return {**message, "_sender": sender}
        except (ValueError, TypeError, InvalidTag, zlib.error, UnicodeError, RecursionError):
            return None

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("Esta conexão já foi encerrada.")
        if self._task is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, connect=15, sock_connect=15, sock_read=None),
                headers={"User-Agent": "Ponte/0.1.0"}, trust_env=True,
            )
            self._task = asyncio.create_task(self._subscribe(), name="ponte-signaling")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=20)
        except TimeoutError:
            await self.close()
            raise RuntimeError("Não foi possível acessar o serviço de conexão. Confira a internet e tente novamente.") from None
        if self._start_error:
            failure = self._start_error
            await self.close()
            raise failure

    async def send(self, target: str, message: dict) -> None:
        wire = self._encode(target, message)
        if self._closed or self._session is None or not self._ready.is_set():
            raise RuntimeError("O serviço de conexão ainda não está disponível.")
        try:
            async with self._session.post(
                self.endpoint + "/" + self._topic(target), data=wire.encode("ascii"),
                headers={"Content-Type": "text/plain; charset=utf-8", "X-Firebase": "no"},
                timeout=aiohttp.ClientTimeout(total=25, connect=15), allow_redirects=False,
            ) as response:
                if response.status == 429:
                    raise RuntimeError("O serviço de conexão atingiu seu limite temporário. Aguarde antes de tentar novamente.")
                if not 200 <= response.status < 300:
                    raise RuntimeError("O serviço não aceitou a negociação da conexão. Tente novamente mais tarde.")
                await response.content.read(8192)
        except (aiohttp.ClientError, TimeoutError, OSError):
            # A failed POST may already have arrived: never retry automatically
            # and accidentally duplicate a handshake or consume daily quota.
            raise RuntimeError("Não foi possível enviar a negociação. Confira a internet e tente novamente.") from None

    async def _subscribe(self) -> None:
        delay = 1.0
        while not self._closed:
            connected_at = time.monotonic()
            try:
                assert self._session is not None
                async with self._session.get(
                    self.endpoint + "/" + self._topic(self.peer_id) + "/json",
                    params={"since": self._cursor}, allow_redirects=False,
                ) as response:
                    if response.status in {400, 401, 403, 404} or 300 <= response.status < 400:
                        self._start_error = RuntimeError("O serviço de conexão recusou o acesso. Verifique seu endereço e tente novamente.")
                        self._status(str(self._start_error))
                        self._ready.set()
                        return
                    if response.status != 200:
                        if response.status == 429:
                            self._status("Limite temporário do serviço de conexão. Aguardando para reconectar…")
                        raise aiohttp.ClientConnectionError("Signaling unavailable")
                    buffer = b""
                    async for chunk in response.content.iter_chunked(4096):
                        if self._closed:
                            return
                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            if len(line) <= 16384:
                                await self._line(line)
                            if self._closed:
                                return
                        if len(buffer) > 16384:
                            raise aiohttp.ClientConnectionError("Invalid signaling stream")
                    if buffer and len(buffer) <= 16384:
                        await self._line(buffer)
                    if not self._closed:
                        raise aiohttp.ClientConnectionError("Signaling stream ended")
            except asyncio.CancelledError:
                return
            except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
                if self._closed:
                    return
                self._status("Reconectando o serviço de conexão…")
            if time.monotonic() - connected_at >= 30:
                delay = 1.0
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _line(self, line: bytes) -> None:
        if self._closed or not line:
            return
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            return
        if not isinstance(event, dict):
            return
        if event.get("event") == "open":
            self._ready.set()
            self._status("Serviço de conexão pronto.")
            return
        if event.get("event") != "message":
            return
        cursor = event.get("id")
        if isinstance(cursor, str) and _MESSAGE_ID.fullmatch(cursor):
            self._cursor = cursor
        message = self._decode(event.get("message"))
        if message is not None:
            try:
                await self.on_message(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._status("Não foi possível processar uma mensagem da conexão.")

    async def close(self) -> None:
        self._closed = True
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._session is not None:
            await self._session.close()
            self._session = None
