"""Authenticated QUIC streams and unreliable datagrams over direct ICE UDP.

The Sans-I/O QUIC engine exchanges packets with ``aioice.Connection`` directly;
there is no SCTP or TCP carrier.  The two ephemeral TLS certificate fingerprints
belong in the authenticated signaling messages.  Both endpoints pin the actual
TLS peer certificate before exposing any application data.

Private aioquic details below are deliberately confined to this adapter and
tested against the pinned aioquic 1.7.0 / aioice 0.10.2 versions.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import re
import ssl
import struct
from datetime import datetime, timedelta, timezone

import aioice
from aioquic.buffer import Buffer
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import (
    CONNECTION_LIMIT_FRAME_CAPACITY, MAX_STREAM_DATA_FRAME_CAPACITY, QuicConnection,
)
from aioquic.quic.events import (
    ConnectionTerminated, DatagramFrameReceived, HandshakeCompleted,
    StopSendingReceived, StreamDataReceived, StreamReset,
)
from aioquic.quic.packet import QuicFrameType, QuicPacketType, pull_quic_header
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pyee.asyncio import AsyncIOEventEmitter

PREFIX = "PONTE-QUIC1\n"
MAX_DESCRIPTION = 128000
MAX_CHANNELS = 33
MAX_FRAME = 16385
MAX_TEXT = 1024
MAX_LABEL = 80
CHANNEL_WINDOW = 262144
CONNECTION_WINDOW = 1048576
MAX_DATAGRAM = 1000
MAX_DATAGRAM_QUEUE = 8
_HEADER = b"PCH1"
_ADDR = ("0.0.0.0", 0)  # ICE owns and validates the actual nominated address.
_ERROR = 0x100


def is_quic_description(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Campo repetido na descrição QUIC.")
        result[key] = value
    return result


def _extensions(value):
    if not isinstance(value, dict):
        raise ValueError("Extensões QUIC inválidas.")
    try:
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        if len(encoded) > 8192:
            raise ValueError()
        # A bounded plain-JSON snapshot, independent of mutable callers.
        return json.loads(encoded)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("Extensões QUIC inválidas.") from exc


def validate_description(value):
    """Parse bounded, IP-only, UDP-only direct ICE signaling; never resolve DNS."""
    try:
        if not is_quic_description(value) or len(value) > MAX_DESCRIPTION:
            raise ValueError()
        data = json.loads(value[len(PREFIX):], object_pairs_hook=_unique_object)
        if (not isinstance(data, dict) or type(data.get("v")) is not int or data["v"] != 1
                or data.get("role") not in ("client", "server")
                or set(data) != {"v", "role", "ice", "certificate_sha256", "extensions"}):
            raise ValueError()
        fingerprint = data["certificate_sha256"]
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError()
        ice = data["ice"]
        if not isinstance(ice, dict) or set(ice) != {"username", "password", "candidates"}:
            raise ValueError()
        for field, minimum in (("username", 4), ("password", 22)):
            item = ice[field]
            if not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9+/]{%d,256}" % minimum, item):
                raise ValueError()
        candidates = ice["candidates"]
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 48:
            raise ValueError()
        normalized = []
        for text in candidates:
            if not isinstance(text, str) or len(text) > 512 or len(text.split()) < 8:
                raise ValueError()
            parts = text.split()
            options = parts[8::2]
            if (len(parts) % 2 or parts[6] != "typ"
                    or not re.fullmatch(r"[A-Za-z0-9+/]{1,64}", parts[0])
                    or len(options) != len(set(options))
                    or any(item not in ("raddr", "rport", "generation") for item in options)):
                raise ValueError()
            candidate = aioice.Candidate.from_sdp(text)
            address = ipaddress.ip_address(candidate.host)
            if (candidate.component != 1 or candidate.transport.lower() != "udp"
                    or candidate.type not in ("host", "srflx", "prflx")
                    or not 1 <= candidate.port <= 65535 or not 0 <= candidate.priority <= 2**32 - 1
                    or address.is_multicast or address.is_unspecified or "%" in candidate.host
                    or candidate.tcptype is not None):
                raise ValueError()
            if candidate.related_address is not None:
                related = ipaddress.ip_address(candidate.related_address)
                if related.version != address.version or "%" in candidate.related_address:
                    raise ValueError()
            if candidate.related_port is not None and not 1 <= candidate.related_port <= 65535:
                raise ValueError()
            normalized.append(candidate.to_sdp())
        ice["candidates"] = normalized
        data["extensions"] = _extensions(data["extensions"])
        return data
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError) as exc:
        raise ValueError("Descrição QUIC direta inválida.") from exc


class _MutualTLSConnection(QuicConnection):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._local_max_streams_bidi.value = self._local_max_streams_bidi.sent = MAX_CHANNELS
        self._local_max_streams_uni.value = self._local_max_streams_uni.sent = 0

    def _initialize(self, peer_cid):
        super()._initialize(peer_cid)
        # aioquic 1.7.0 verifies CertificateVerify but its public configuration
        # does not request client certificates. Pinning after the handshake also
        # rejects a client that responds to CertificateRequest with no cert.
        if not self.configuration.is_client:
            self.tls._request_client_certificate = True

    def _write_connection_limits(self, builder, space):
        # aioquic's default doubles credit when the highest received offset
        # passes halfway. A peer can exploit sparse offsets to grow native
        # reassembly indefinitely. Grant a sliding window based on contiguous
        # delivery instead. Retired/reset streams are counted until discarded.
        retained = sum(max(0, stream.receiver.highest_offset - stream.receiver._buffer_start)
                       for stream in self._streams.values())
        credit = self._local_max_data.used - retained + CONNECTION_WINDOW
        if credit - self._local_max_data.value >= CONNECTION_WINDOW // 2:
            self._local_max_data.value = credit
        for limit in (self._local_max_data, self._local_max_streams_bidi, self._local_max_streams_uni):
            if (limit is not self._local_max_data
                    and limit.sent - limit.used >= limit.value - limit.sent):
                continue
            if limit.sent != limit.value:
                output = builder.start_frame(limit.frame_type, capacity=CONNECTION_LIMIT_FRAME_CAPACITY,
                                             handler=self._on_connection_limit_delivery, handler_args=(limit,))
                output.push_uint_var(limit.value)
                limit.sent = limit.value

    def _write_stream_limits(self, builder, space, stream):
        credit = stream.receiver._buffer_start + CHANNEL_WINDOW
        if stream.max_stream_data_local and credit - stream.max_stream_data_local >= CHANNEL_WINDOW // 2:
            stream.max_stream_data_local = credit
        if stream.max_stream_data_local != stream.max_stream_data_local_sent:
            output = builder.start_frame(QuicFrameType.MAX_STREAM_DATA, capacity=MAX_STREAM_DATA_FRAME_CAPACITY,
                                         handler=self._on_max_stream_data_delivery, handler_args=(stream,))
            output.push_uint_var(stream.stream_id)
            output.push_uint_var(stream.max_stream_data_local)
            stream.max_stream_data_local_sent = stream.max_stream_data_local


class QuicChannel(AsyncIOEventEmitter):
    """Small data-channel-compatible view of one independent QUIC stream."""
    def __init__(self, peer, label, stream_id=None):
        super().__init__()
        self.peer = peer
        self.label = label
        self.ordered = True
        self.readyState = "connecting"
        self.bufferedAmountLowThreshold = 0
        self._stream_id = stream_id
        self._incoming = bytearray()
        self._header_received = stream_id is None
        self._buffered = 0

    @property
    def bufferedAmount(self):
        if self._stream_id is None or self.peer._quic is None:
            return 0
        stream = self.peer._quic._streams.get(self._stream_id)
        if stream is None or stream.sender.is_finished:
            return 0
        # Match data-channel bufferedAmount: unsent bytes, including ranges
        # waiting for retransmission. Bytes already on the wire belong to the
        # transport congestion window, not the application's small send gate.
        if stream.sender._reset_error_code is not None:
            return 0
        return sum(item.stop - item.start for item in stream.sender._pending)

    @property
    def unackedAmount(self):
        if self._stream_id is None or self.peer._quic is None:
            return 0
        stream = self.peer._quic._streams.get(self._stream_id)
        if stream is None:
            return 0
        return max(0, stream.sender._buffer_stop - stream.sender._buffer_start)

    def send(self, value):
        if self.readyState != "open" or not self.peer._authenticated:
            raise ConnectionError("O canal QUIC ainda não está aberto.")
        if isinstance(value, str):
            payload, kind, maximum = value.encode("utf8"), 1, MAX_TEXT
        elif isinstance(value, bytes):
            payload, kind, maximum = value, 0, MAX_FRAME
        else:
            raise TypeError("O canal aceita somente bytes ou texto.")
        if len(payload) > maximum:
            raise ValueError("Mensagem QUIC acima do limite.")
        frame = struct.pack("!BI", kind, len(payload)) + payload
        if (self.unackedAmount + len(frame) > CHANNEL_WINDOW
                or self.peer.unackedAmount + len(frame) > CONNECTION_WINDOW):
            raise BufferError("A janela de envio QUIC está cheia.")
        self.peer._quic.send_stream_data(self._stream_id, frame)
        self._buffered = self.bufferedAmount
        self.peer._wakeup.set()

    def close(self):
        if self.readyState in ("closing", "closed"):
            return
        if self._stream_id is None or self.peer._quic is None:
            self._closed()
            return
        self.readyState = "closing"
        # Graceful stream FIN follows all preceding stream bytes, including FIN
        # acknowledgments carried by StreamBridge's application protocol.
        self.peer._quic.send_stream_data(self._stream_id, b"", end_stream=True)
        self.peer._wakeup.set()

    def _closed(self):
        if self.readyState == "closed":
            return
        self.readyState = "closed"
        self._incoming.clear()
        self.peer._channels.discard(self)
        if self._stream_id is not None:
            self.peer._streams.pop(self._stream_id, None)
        self.emit("close")

    def _abort(self):
        if self._stream_id is not None and self.peer._quic is not None:
            with contextlib.suppress(ValueError):
                self.peer._quic.reset_stream(self._stream_id, _ERROR)
                self.peer._quic.stop_stream(self._stream_id, _ERROR)
            self.peer._wakeup.set()
        self._closed()

    def _receive(self, data, ended):
        if self.readyState == "closed":
            return
        if len(self._incoming) + len(data) > CHANNEL_WINDOW:
            self._abort()
            return
        self._incoming.extend(data)
        if not self._header_received:
            if len(self._incoming) < 6:
                if ended:
                    self._abort()
                return
            size = struct.unpack("!H", self._incoming[4:6])[0]
            if self._incoming[:4] != _HEADER or not 1 <= size <= MAX_LABEL:
                self._abort()
                return
            if len(self._incoming) < 6 + size:
                if ended:
                    self._abort()
                return
            try:
                self.label = bytes(self._incoming[6:6 + size]).decode("utf8")
            except UnicodeError:
                self._abort()
                return
            if not self.label.isprintable():
                self._abort()
                return
            del self._incoming[:6 + size]
            self._header_received = True
            self.readyState = "open"
            # Register listeners before emitting messages bundled in this same
            # UDP packet. The remote application can reject the channel here.
            self.peer.emit("datachannel", self)
            if self.readyState == "closed":
                return
            self.emit("open")
        while len(self._incoming) >= 5 and self.readyState != "closed":
            kind, size = struct.unpack("!BI", self._incoming[:5])
            if kind not in (0, 1) or size > (MAX_TEXT if kind else MAX_FRAME):
                self._abort()
                return
            if len(self._incoming) < 5 + size:
                break
            payload = bytes(self._incoming[5:5 + size])
            del self._incoming[:5 + size]
            try:
                self.emit("message", payload.decode("utf8") if kind else payload)
            except UnicodeError:
                self._abort()
                return
        if ended:
            if self._incoming:
                self._abort()
            else:
                if self.readyState == "open":
                    self.close()
                self._closed()


class QuicPeerConnection(AsyncIOEventEmitter):
    """One direct ICE path, mutual TLS, independent reliable streams and DATAGRAM."""
    def __init__(self, stun_server=("stun.cloudflare.com", 3478), *, use_ipv4=True, use_ipv6=True):
        super().__init__()
        self.connectionState = "new"
        self.signalingState = "stable"
        self.extensions = {}
        self.remote_extensions = {}
        self._stun_server = stun_server
        self._use_ipv4, self._use_ipv6 = use_ipv4, use_ipv6
        self._ice = None
        self._quic = None
        self._channels = set()
        self._streams = {}
        self._is_client = None
        self._remote = None
        self._gather_task = None
        self._run_task = None
        self._close_task = None
        self._closing = False
        self._authenticated = False
        self._failure = None
        self._wakeup = asyncio.Event()
        self._datagrams_dropped = 0
        self._datagrams_sent = 0
        self._datagrams_received = 0
        self._key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Ponte ephemeral peer")])
        now = datetime.now(timezone.utc)
        self._certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                             .public_key(self._key.public_key()).serial_number(x509.random_serial_number())
                             .not_valid_before(now - timedelta(minutes=5))
                             .not_valid_after(now + timedelta(days=1)).sign(self._key, hashes.SHA256()))

    @property
    def bufferedAmount(self):
        return sum(channel.bufferedAmount for channel in self._channels)

    @property
    def unackedAmount(self):
        # Count every native stream, including locally retired/reset streams
        # whose buffers remain retained until QUIC receives the final ACK.
        if self._quic is None:
            return 0
        return sum(max(0, stream.sender._buffer_stop - stream.sender._buffer_start)
                   for stream in self._quic._streams.values())

    def _state(self, state):
        if self.connectionState != state:
            self.connectionState = state
            self.emit("connectionstatechange")

    def createDataChannel(self, label, ordered=True):
        if self._closing:
            raise ConnectionError("A conexão QUIC está fechada.")
        if (ordered is not True or not isinstance(label, str) or not label.isprintable()
                or not 1 <= len(label.encode("utf8")) <= MAX_LABEL):
            raise ValueError("Canal QUIC inválido.")
        if len(self._channels) >= MAX_CHANNELS:
            raise BufferError("Limite de canais QUIC atingido.")
        channel = QuicChannel(self, label)
        self._channels.add(channel)
        if self._authenticated:
            self._open_channel(channel)
        return channel

    def _open_channel(self, channel):
        if self._closing or channel.readyState != "connecting":
            return
        stream_id = self._quic.get_next_available_stream_id()
        channel._stream_id = stream_id
        self._streams[stream_id] = channel
        label = channel.label.encode("utf8")
        self._quic.send_stream_data(stream_id, _HEADER + struct.pack("!H", len(label)) + label)
        channel.readyState = "open"
        channel._buffered = channel.bufferedAmount
        self._wakeup.set()
        channel.emit("open")

    async def _gather(self, is_client):
        if self._closing or self._ice is not None:
            raise ConnectionError("Negociação QUIC indisponível.")
        self._is_client = is_client
        self._ice = aioice.Connection(ice_controlling=is_client, components=1,
                                      stun_server=self._stun_server, use_ipv4=self._use_ipv4,
                                      use_ipv6=self._use_ipv6)
        # Shield gathering: aioice registers transports after binding sockets.
        # Teardown owns this task and lets registration finish before closing
        # them, even if the signaling caller is cancelled midway through it.
        self._gather_task = asyncio.create_task(self._ice.gather_candidates())
        await asyncio.shield(self._gather_task)
        if self._closing:
            raise ConnectionError("A conexão QUIC foi encerrada.")
        candidates = []
        for candidate in self._ice.local_candidates:
            with contextlib.suppress(ValueError):
                address = ipaddress.ip_address(candidate.host)
                if (candidate.type in ("host", "srflx", "prflx") and "%" not in candidate.host
                        and not address.is_unspecified and not address.is_multicast):
                    candidates.append(candidate.to_sdp())
        description = {"v": 1, "role": "client" if is_client else "server",
                       "ice": {"username": self._ice.local_username, "password": self._ice.local_password,
                               "candidates": candidates[:48]},
                       "certificate_sha256": self._certificate.fingerprint(hashes.SHA256()).hex(),
                       "extensions": _extensions(self.extensions)}
        encoded = PREFIX + json.dumps(description, sort_keys=True, separators=(",", ":"))
        validate_description(encoded)
        return encoded

    async def offer(self):
        result = await self._gather(True)
        self.signalingState = "have-local-offer"
        return result

    async def answer(self, remote):
        description = validate_description(remote)
        if description["role"] != "client":
            raise ValueError("Oferta QUIC inválida.")
        result = await self._gather(False)
        await self._set_remote(description)
        self.signalingState = "stable"
        self._start()
        return result

    async def accept_answer(self, remote):
        description = validate_description(remote)
        if self._is_client is not True or self.signalingState != "have-local-offer" or description["role"] != "server":
            raise ValueError("Resposta QUIC inválida.")
        await self._set_remote(description)
        self.signalingState = "stable"
        self._start()

    async def _set_remote(self, description):
        if self._closing:
            raise ConnectionError("A conexão QUIC foi encerrada.")
        self._remote = description
        self.remote_extensions = description["extensions"]
        self._ice.remote_username = description["ice"]["username"]
        self._ice.remote_password = description["ice"]["password"]
        for text in description["ice"]["candidates"]:
            await self._ice.add_remote_candidate(aioice.Candidate.from_sdp(text))
        await self._ice.add_remote_candidate(None)

    def _configuration(self):
        return QuicConfiguration(is_client=self._is_client, alpn_protocols=["ponte/1"],
                                 congestion_control_algorithm="cubic", idle_timeout=45,
                                 max_data=CONNECTION_WINDOW, max_stream_data=CHANNEL_WINDOW,
                                 max_datagram_frame_size=1200, max_datagram_size=1200,
                                 certificate=self._certificate, private_key=self._key,
                                 verify_mode=ssl.CERT_NONE)

    def _start(self):
        if self._closing or self._run_task is not None:
            raise ConnectionError("Negociação QUIC indisponível.")
        self._state("connecting")
        self._run_task = asyncio.create_task(self._run())

    async def _flush(self):
        if self._quic is not None:
            for packet, _ in self._quic.datagrams_to_send(now=asyncio.get_running_loop().time()):
                await self._ice.send(packet)

    def _process_events(self):
        while self._quic is not None:
            event = self._quic.next_event()
            if event is None:
                break
            if isinstance(event, HandshakeCompleted):
                certificate = self._quic.tls._peer_certificate
                if (event.alpn_protocol != "ponte/1" or certificate is None
                        or certificate.fingerprint(hashes.SHA256()).hex() != self._remote["certificate_sha256"]):
                    raise ConnectionError("A identidade TLS QUIC não corresponde ao convite aprovado.")
                self._authenticated = True
                self._state("connected")
                for channel in list(self._channels):
                    self._open_channel(channel)
            elif isinstance(event, ConnectionTerminated):
                raise ConnectionError("A conexão QUIC remota terminou.")
            elif isinstance(event, StreamDataReceived):
                if not self._authenticated:
                    raise ConnectionError("Dados QUIC recebidos antes da autenticação.")
                channel = self._streams.get(event.stream_id)
                if channel is None:
                    # Only remotely initiated bidirectional streams can open a
                    # channel; bounded even if the peer opens empty streams.
                    if (event.stream_id & 2 or (event.stream_id & 1) == int(not self._is_client)
                            or len(self._channels) >= MAX_CHANNELS):
                        with contextlib.suppress(ValueError):
                            self._quic.stop_stream(event.stream_id, _ERROR)
                            self._quic.reset_stream(event.stream_id, _ERROR)
                        continue
                    channel = QuicChannel(self, "", event.stream_id)
                    self._channels.add(channel)
                    self._streams[event.stream_id] = channel
                channel._receive(event.data, event.end_stream)
            elif isinstance(event, (StreamReset, StopSendingReceived)):
                channel = self._streams.get(event.stream_id)
                if channel is not None:
                    channel._abort()
            elif isinstance(event, DatagramFrameReceived):
                if not self._authenticated:
                    raise ConnectionError("Datagrama QUIC recebido antes da autenticação.")
                if len(event.data) <= MAX_DATAGRAM:
                    self._datagrams_received += 1
                    self.emit("datagram", event.data)

    def _update_buffers(self):
        for channel in list(self._channels):
            amount = channel.bufferedAmount
            if channel._buffered > channel.bufferedAmountLowThreshold >= amount:
                channel.emit("bufferedamountlow")
            channel._buffered = amount
            if channel.readyState == "closing":
                stream = self._quic._streams.get(channel._stream_id)
                if stream is None or stream.sender.is_finished:
                    channel._closed()

    async def _run(self):
        receive = wake = None
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(self._ice.connect(), 25)
            if self._closing:
                return
            if self._is_client:
                self._quic = _MutualTLSConnection(configuration=self._configuration())
                self._quic.connect(_ADDR, now=loop.time())
            receive = asyncio.create_task(self._ice.recv())
            handshake_deadline = loop.time() + 15
            while not self._closing:
                self._process_events()
                self._update_buffers()
                self._wakeup.clear()
                await self._flush()
                self._update_buffers()
                timer = self._quic.get_timer() if self._quic is not None else None
                if not self._authenticated:
                    timer = min(timer, handshake_deadline) if timer is not None else handshake_deadline
                timeout = max(0, timer - loop.time()) if timer is not None else None
                wake = asyncio.create_task(self._wakeup.wait())
                done, _ = await asyncio.wait((receive, wake), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                if wake not in done:
                    wake.cancel()
                    await asyncio.gather(wake, return_exceptions=True)
                wake = None
                if not self._authenticated and loop.time() >= handshake_deadline:
                    raise ConnectionError("A autenticação QUIC excedeu o tempo limite.")
                if receive in done:
                    packet = receive.result()
                    if self._quic is None:
                        try:
                            header = pull_quic_header(Buffer(data=packet), host_cid_length=8)
                        except ValueError:
                            receive = asyncio.create_task(self._ice.recv())
                            continue
                        if header.packet_type != QuicPacketType.INITIAL or len(packet) < 1200:
                            receive = asyncio.create_task(self._ice.recv())
                            continue
                        self._quic = _MutualTLSConnection(configuration=self._configuration(),
                                                         original_destination_connection_id=header.destination_cid)
                    self._quic.receive_datagram(packet, _ADDR, now=loop.time())
                    receive = asyncio.create_task(self._ice.recv())
                elif self._quic is not None and timer is not None and loop.time() >= timer:
                    self._quic.handle_timer(now=loop.time())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closing:
                self._failure = exc
                self._state("failed")
        finally:
            for task in (receive, wake):
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(task for task in (receive, wake) if task is not None), return_exceptions=True)
            if not self._closing:
                # Own the shutdown task. Failure must release ICE sockets even
                # when the caller has no listener for connectionstatechange.
                self._closing = True
                self._close_task = asyncio.create_task(self._shutdown(preserve_failed=True))

    def send_datagram(self, payload):
        if not isinstance(payload, bytes) or len(payload) > MAX_DATAGRAM:
            raise ValueError("Datagrama QUIC inválido (máximo de 1000 bytes).")
        quic = self._quic
        if (self._closing or not self._authenticated or quic is None
                or len(quic._datagrams_pending) >= MAX_DATAGRAM_QUEUE
                or quic._loss.bytes_in_flight + (len(quic._datagrams_pending) + 1) * 1200 > quic._loss.congestion_window):
            self._datagrams_dropped += 1
            return False
        quic.send_datagram_frame(payload)
        self._datagrams_sent += 1
        self._wakeup.set()
        return True

    def selected_route(self):
        pair = self._ice._nominated.get(1) if self._ice is not None else None
        if pair is None:
            return ""
        return f"{pair.local_candidate.type} ↔ {pair.remote_candidate.type} · UDP / QUIC"

    def diagnostics(self):
        loss = self._quic._loss if self._quic is not None else None
        return {"transport": "quic", "state": self.connectionState,
                "rtt_ms": round(loss._rtt_smoothed * 1000, 2) if loss is not None and loss._rtt_initialized else None,
                "congestion_window": loss.congestion_window if loss is not None else 0,
                "bytes_in_flight": loss.bytes_in_flight if loss is not None else 0,
                "buffered_bytes": self.bufferedAmount, "unacked_bytes": self.unackedAmount,
                "channels": len(self._channels),
                "datagrams_sent": self._datagrams_sent, "datagrams_received": self._datagrams_received,
                "datagrams_dropped": self._datagrams_dropped}

    async def _shutdown(self, preserve_failed=False):
        if self._gather_task is not None:
            # aioice's STUN gathering has its own five-second bound.
            await asyncio.gather(self._gather_task, return_exceptions=True)
        if self._run_task is not None and self._run_task is not asyncio.current_task():
            self._run_task.cancel()
            await asyncio.gather(self._run_task, return_exceptions=True)
        if self._ice is not None:
            # Cancel connectivity checks before any UDP transport is closed.
            checks = [pair.task for pair in self._ice._check_list if pair.task is not None]
            for task in checks:
                task.cancel()
            await asyncio.gather(*checks, return_exceptions=True)
            if self._quic is not None:
                self._quic.close(error_code=_ERROR if preserve_failed else 0)
                with contextlib.suppress(Exception):
                    await self._flush()
            await self._ice.close()
            checks = [pair.task for pair in self._ice._check_list if pair.task is not None]
            for task in checks:
                task.cancel()
            await asyncio.gather(*checks, return_exceptions=True)
        self._authenticated = False
        for channel in list(self._channels):
            channel._closed()
        if not preserve_failed:
            self._state("closed")
        self.signalingState = "closed"

    async def close(self):
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._shutdown())
        await asyncio.shield(self._close_task)
        if self.connectionState != "closed":
            self._state("closed")
