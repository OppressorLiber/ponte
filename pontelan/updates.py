"""Signed, bounded updates from the public GitHub release repository."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urljoin, urlsplit

import aiohttp
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MAX_PACKAGE_BYTES = 300 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_SIGNATURE_BYTES = 512
MAX_API_BYTES = 1024 * 1024
_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ASSET_HOSTS = frozenset({"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"})
_METADATA_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10, sock_read=15)
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=600, connect=10, sock_read=30)
_HEADERS = {"Accept": "application/vnd.github+json", "Accept-Encoding": "identity", "User-Agent": "Ponte-Updater"}


class UpdateError(RuntimeError):
    """An update cannot be authenticated, obtained, or safely stored."""


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    url: str
    size: int
    sha256: str
    notes: str
    manifest_bytes: bytes
    signature: bytes
    asset_name: str = "Ponte.exe"


def _version(value: object) -> tuple[int, int, int]:
    if not isinstance(value, str) or len(value) > 64 or not _VERSION.fullmatch(value):
        raise UpdateError("A versão da atualização é inválida.")
    return tuple(int(part) for part in value.split("."))


def semver_compare(left: str, right: str) -> int:
    """Compare canonical, stable MAJOR.MINOR.PATCH versions numerically."""
    first, second = _version(left), _version(right)
    return (first > second) - (first < second)


def _config(config: dict) -> tuple[str, Ed25519PublicKey]:
    repository = config.get("repository") if isinstance(config, dict) else None
    if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository) or repository.split("/")[1] in (".", ".."):
        raise UpdateError("O repositório de atualizações não está configurado.")
    encoded_key = config.get("public_key")
    try:
        raw_key = base64.b64decode(encoded_key, validate=True) if isinstance(encoded_key, str) else b""
        if len(raw_key) != 32:
            raise ValueError("invalid key length")
        return repository, Ed25519PublicKey.from_public_bytes(raw_key)
    except (ValueError, binascii.Error) as exc:
        raise UpdateError("A chave de atualizações não está configurada corretamente.") from exc


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _json(data: bytes) -> object:
    try:
        return json.loads(data, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise UpdateError("O servidor retornou dados de atualização inválidos.") from exc


def _asset_url(repository: str, version: str, name: str) -> str:
    return f"https://github.com/{repository}/releases/download/v{version}/{name}"


def verify_manifest(
    manifest_bytes: bytes,
    signature: bytes,
    config: dict,
    expected_version: str | None = None,
) -> UpdateInfo:
    """Verify a raw Ed25519 signature and validate the complete signed manifest."""
    repository, public_key = _config(config)
    if not isinstance(manifest_bytes, bytes) or not 0 < len(manifest_bytes) <= MAX_MANIFEST_BYTES:
        raise UpdateError("O manifesto da atualização excede o tamanho permitido.")
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise UpdateError("A assinatura da atualização é inválida.")
    try:
        public_key.verify(signature, manifest_bytes)
    except InvalidSignature as exc:
        raise UpdateError("A assinatura da atualização não confere. O arquivo foi recusado.") from exc
    manifest = _json(manifest_bytes)
    if not isinstance(manifest, dict) or type(manifest.get("schema")) is not int or manifest["schema"] != 1:
        raise UpdateError("O formato do manifesto da atualização não é suportado.")
    version = manifest.get("version")
    _version(version)
    if manifest.get("repository") != repository or (expected_version is not None and version != expected_version):
        raise UpdateError("O manifesto não corresponde ao repositório ou à versão da atualização.")
    package = manifest.get("windows")
    notes = manifest.get("notes")
    if not isinstance(package, dict) or not isinstance(notes, str):
        raise UpdateError("O manifesto da atualização está incompleto.")
    size, checksum, url = package.get("size"), package.get("sha256"), package.get("url")
    if type(size) is not int or not 0 < size <= MAX_PACKAGE_BYTES:
        raise UpdateError("O tamanho da atualização é inválido.")
    if not isinstance(checksum, str) or not _SHA256.fullmatch(checksum):
        raise UpdateError("A identificação SHA-256 da atualização é inválida.")
    if url != _asset_url(repository, version, "Ponte.exe"):
        raise UpdateError("O endereço da atualização não corresponde à publicação oficial.")
    return UpdateInfo(version, url, size, checksum, notes, manifest_bytes, signature)


def _validate_url(url: str, hosts: frozenset[str]) -> None:
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https" and parsed.hostname in hosts
            and parsed.username is None and parsed.password is None
            and parsed.port in (None, 443) and not parsed.fragment
            and not any(char.isspace() or ord(char) < 32 for char in url)
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise UpdateError("O servidor tentou usar um endereço de atualização não autorizado.")


@asynccontextmanager
async def _using_session(session: aiohttp.ClientSession | None) -> AsyncIterator[aiohttp.ClientSession]:
    if session is not None:
        yield session
    else:
        async with aiohttp.ClientSession(trust_env=False) as owned:
            yield owned


@asynccontextmanager
async def _response(
    session: aiohttp.ClientSession,
    url: str,
    hosts: frozenset[str],
    timeout: aiohttp.ClientTimeout,
) -> AsyncIterator[aiohttp.ClientResponse]:
    for redirect in range(6):
        _validate_url(url, hosts)
        async with session.get(url, allow_redirects=False, timeout=timeout, headers=_HEADERS,
                               auto_decompress=False, max_line_size=65536, max_field_size=65536) as response:
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location or redirect == 5:
                    raise UpdateError("O servidor de atualizações redirecionou a solicitação muitas vezes.")
                url = urljoin(url, location)
                continue
            yield response
            return


def _status(response: aiohttp.ClientResponse) -> None:
    if response.status == 200:
        return
    if response.status in (403, 429):
        raise UpdateError("O GitHub atingiu um limite temporário. Tente verificar novamente mais tarde.")
    if response.status == 404:
        raise UpdateError("Um arquivo da atualização não está disponível no GitHub.")
    raise UpdateError(f"O GitHub não conseguiu fornecer a atualização (HTTP {response.status}).")


async def _read_limited(response: aiohttp.ClientResponse, limit: int) -> bytes:
    if response.content_length is not None and response.content_length > limit:
        raise UpdateError("A resposta da atualização excede o tamanho permitido.")
    data = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        data.extend(chunk)
        if len(data) > limit:
            raise UpdateError("A resposta da atualização excede o tamanho permitido.")
    return bytes(data)


async def _asset_bytes(session: aiohttp.ClientSession, url: str, limit: int) -> bytes:
    async with _response(session, url, _ASSET_HOSTS, _METADATA_TIMEOUT) as response:
        _status(response)
        return await _read_limited(response, limit)


async def discover_update(
    current_version: str,
    config: dict,
    session: aiohttp.ClientSession | None = None,
) -> UpdateInfo | None:
    """Find and authenticate a newer stable release, or return None."""
    repository, _ = _config(config)
    _version(current_version)
    try:
        async with _using_session(session) as client:
            api_url = f"https://api.github.com/repos/{repository}/releases/latest"
            async with _response(client, api_url, frozenset({"api.github.com"}), _METADATA_TIMEOUT) as response:
                if response.status == 404:
                    return None
                _status(response)
                release = _json(await _read_limited(response, MAX_API_BYTES))
            if not isinstance(release, dict) or release.get("draft") is not False or release.get("prerelease") is not False:
                raise UpdateError("A publicação retornada não é uma versão estável da Ponte.")
            tag = release.get("tag_name")
            if not isinstance(tag, str) or not tag.startswith("v"):
                raise UpdateError("A publicação possui uma versão inválida.")
            version = tag[1:]
            if semver_compare(version, current_version) <= 0:
                return None
            assets = release.get("assets")
            if not isinstance(assets, list):
                raise UpdateError("A publicação não contém os arquivos de autenticação da atualização.")
            for name in ("update.json", "update.sig"):
                matching = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == name]
                if len(matching) != 1 or matching[0].get("browser_download_url") != _asset_url(repository, version, name):
                    raise UpdateError("A publicação não contém arquivos oficiais de autenticação válidos.")
            manifest_bytes = await _asset_bytes(client, _asset_url(repository, version, "update.json"), MAX_MANIFEST_BYTES)
            signature_text = await _asset_bytes(client, _asset_url(repository, version, "update.sig"), MAX_SIGNATURE_BYTES)
            try:
                signature = base64.b64decode(signature_text.strip(), validate=True)
            except (ValueError, binascii.Error) as exc:
                raise UpdateError("A assinatura da atualização é inválida.") from exc
            return verify_manifest(manifest_bytes, signature, config, version)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise UpdateError("Não foi possível consultar atualizações. Confira a conexão e tente novamente.") from exc


async def download_update(
    info: UpdateInfo,
    destination: Path,
    on_progress: Callable[[int, int], None] | None = None,
    session: aiohttp.ClientSession | None = None,
) -> Path:
    """Download to a unique .part file and replace destination after size/hash validation."""
    _version(info.version)
    _validate_url(info.url, _ASSET_HOSTS)
    parsed = urlsplit(info.url)
    parts = parsed.path.split("/")
    repository = "/".join(parts[1:3])
    if not _REPOSITORY.fullmatch(repository) or repository.split("/")[1] in (".", "..") or info.url != _asset_url(repository, info.version, "Ponte.exe"):
        raise UpdateError("O endereço da atualização não corresponde à publicação oficial.")
    if type(info.size) is not int or not 0 < info.size <= MAX_PACKAGE_BYTES or not isinstance(info.sha256, str) or not _SHA256.fullmatch(info.sha256):
        raise UpdateError("Os dados de verificação da atualização são inválidos.")
    destination = Path(destination)
    temporary = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        async with _using_session(session) as client:
            async with _response(client, info.url, _ASSET_HOSTS, _DOWNLOAD_TIMEOUT) as response:
                _status(response)
                if response.content_length is not None and response.content_length != info.size:
                    raise UpdateError("O tamanho do arquivo recebido não corresponde à atualização assinada.")
                checksum = hashlib.sha256()
                received = 0
                with tempfile.NamedTemporaryFile(mode="wb", dir=destination.parent, prefix=destination.name + ".", suffix=".part", delete=False) as stream:
                    temporary = Path(stream.name)
                    if on_progress is not None:
                        on_progress(0, info.size)
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        received += len(chunk)
                        if received > info.size:
                            raise UpdateError("O arquivo recebido excede o tamanho da atualização assinada.")
                        stream.write(chunk)
                        checksum.update(chunk)
                        if on_progress is not None:
                            on_progress(received, info.size)
                    if received != info.size or checksum.hexdigest() != info.sha256:
                        raise UpdateError("A verificação SHA-256 ou o tamanho da atualização não confere. O arquivo foi recusado.")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
                temporary = None
        return destination
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise UpdateError("O download foi interrompido. Confira a conexão e tente novamente.") from exc
    except OSError as exc:
        raise UpdateError("Não foi possível salvar a atualização. Confira o espaço livre e a permissão da pasta.") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
