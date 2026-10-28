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

from aiortc import RTCConfiguration, RTCIceServer, RTCSessionDescription
from aiortc.sdp import SessionDescription

from .signaling import SignalBus, new_code, normalize_code
from .latency import LatencySamples, summary
from .flow import DeliveryWindow
from .send_buffer import SendBuffer
from .rtc import RoomPeerConnection as RTCPeerConnection
from .identity import Identity, TrustStore, verify_offer
from .quic_transport import QuicPeerConnection, is_quic_description, validate_description
from .ports import UdpForwarder, normalize_ports

LOG = logging.getLogger(__name__)
CHUNK = 16384
SEND_CHUNK = 4096
PEER_SEND_BUDGET = 131072
WINDOW = 262144
MAX_PEERS = 8
MAX_PENDING = 8
MAX_REQUEST_IDS = 1000
APPROVAL_TIMEOUT = 120
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


def admission_description(sdp):
    if is_quic_description(sdp):
        if validate_description(sdp)["role"] != "client":
            raise ValueError("Oferta QUIC inválida.")
        return sdp
    sdp = direct_description(sdp)
    try:
        description = SessionDescription.parse(sdp)
        if not sdp.startswith("v=0") or len(description.media) != 1:
            raise ValueError()
        media = description.media[0]
        if (media.kind != "application" or not media.sctp_port or not media.ice.usernameFragment
                or not media.ice.password or not media.dtls or not media.dtls.fingerprints):
            raise ValueError()
    except (ValueError, AssertionError, IndexError, AttributeError, KeyError) as exc:
        raise ValueError("Descrição do pedido de entrada inválida.") from exc
    return sdp


def outgoing_buffered(peer):
    channels = [stream.channel for stream in peer.streams]
    if getattr(peer, "control", None) is not None:
        channels.append(peer.control)
    return sum(channel.bufferedAmount for channel in channels if channel.readyState == "open")


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
        if not hasattr(peer, "send_buffer"):
            peer.send_buffer = SendBuffer(lambda: outgoing_buffered(peer))
        self.queued_bytes = 0
        self.queued_frames = 0
        self.eof_acked = asyncio.Event()
        self.received_eof = False
        self.sent_eof = False
        self.closed = False
        self.task = None
        channel.bufferedAmountLowThreshold = 0
        channel.on("bufferedamountlow", peer.send_buffer.changed.set)
        channel.on("close", peer.send_buffer.changed.set)
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
            if self.closed or self.peer.closed or getattr(self.peer.owner, "stopping", False):
                return
            if self.reader is None:
                self.reader, self.writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", host_port), 5
                )
            if self.closed or self.peer.closed or getattr(self.peer.owner, "stopping", False):
                return
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
            sent_at = await self.peer.send_buffer.send(self.channel, b"\x00" + data)
            self.pending_times.append(sent_at)
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
        self.peer.send_buffer.retire(self.channel)
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
    def __init__(self, owner, peer_id, name, *, transport=None):
        self.owner, self.peer_id, self.name = owner, peer_id, safe_name(name)
        self.transport = transport or getattr(owner, "transport", "webrtc")
        if self.transport == "quic":
            self.pc = QuicPeerConnection(stun_server=("stun.cloudflare.com", 3478) if owner.stun else None)
        else:
            configuration = RTCConfiguration(iceServers=[RTCIceServer(urls=STUN)] if owner.stun else [])
            self.pc = RTCPeerConnection(configuration)
        self.control = None
        self.streams = set()
        self.delivery = DeliveryWindow(PEER_SEND_BUDGET, quantum=SEND_CHUNK)
        self.send_buffer = SendBuffer(lambda: outgoing_buffered(self))
        self.ready = asyncio.Event()
        self.answer_received = asyncio.Event()
        self.closed = False
        self.rtt_ms = None
        self.latency = LatencySamples()
        self.ping_task = None
        self.connect_task = None
        self.negotiation_task = None
        self.public_key = None
        self.fingerprint = ""
        self.port_mappings = []
        self.extra_servers = []
        self.udp = None
        self._close_task = None
        self.pc.on("datachannel", self._datachannel)
        self.pc.on("connectionstatechange", self._state)
        if self.transport == "quic":
            self.pc.on("datagram", self._datagram)

    def notify(self, state):
        self.rtt_ms = self.latency.latest()
        stats = self.latency.diagnostics()
        self.owner.emit({"type": "peer", "peer_id": self.peer_id, "name": self.name,
                         "state": state, "rtt_ms": self.rtt_ms, "route": self.route(),
                         "transport": self.transport,
                         "transport_name": "QUIC / TLS 1.3" if self.transport == "quic" else "WebRTC / SCTP",
                         "fingerprint": self.fingerprint, "can_trust": bool(self.public_key),
                         "trusted": self.owner._is_trusted(self.public_key, self.fingerprint),
                         "rtt_p50_ms": stats["p50_ms"], "rtt_p95_ms": stats["p95_ms"],
                         "rtt_sample_age": stats["sample_age_seconds"]})

    def route(self):
        if self.transport == "quic":
            return self.pc.selected_route()
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
        if self.closed or self.owner.stopping:
            channel.close()
            return
        if channel.label == "control" and self.control is None:
            self._control(channel)
        elif (self.owner.role == "host" and self.ready.is_set() and len(self.streams) < MAX_STREAMS
              and (match := re.fullmatch(r"tcp:[a-f0-9]{32}(?::(p[1-8]))?", channel.label))):
            mapping_id = match[1]
            target = self.owner.host_port
            if mapping_id:
                mapping = next((p for p in self.port_mappings
                                if p["id"] == mapping_id and p["protocol"] == "tcp"), None)
                if self.transport != "quic" or mapping is None:
                    channel.close()
                    return
                target = mapping["host_port"]
            bridge = StreamBridge(self, channel)
            self.streams.add(bridge)
            bridge.task = asyncio.create_task(bridge.run(target))
        else:
            channel.close()

    def send_datagram(self, data):
        if self.closed or self.owner.stopping or not self.ready.is_set() or self.transport != "quic":
            return False
        return self.pc.send_datagram(data)

    def _datagram(self, data):
        if self.udp and not self.closed and not self.owner.stopping and self.ready.is_set():
            self.udp.handle_datagram(data)

    def _udp_bytes(self, *, sent=0, received=0):
        self.owner.sent += sent
        self.owner.received += received

    def _control(self, channel):
        self.control = channel
        channel.bufferedAmountLowThreshold = 0
        channel.on("bufferedamountlow", self.send_buffer.changed.set)
        channel.on("close", self.send_buffer.changed.set)
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
        if self.transport == "quic":
            return await self.pc.offer()
        await asyncio.wait_for(self.pc.setLocalDescription(await self.pc.createOffer()), 18)
        return direct_description(self.pc.localDescription.sdp)

    async def answer(self, sdp):
        if self.transport == "quic":
            self.port_mappings = list(self.owner.extra_ports)
            self.pc.extensions = {"ports": self.port_mappings}
            self.udp = UdpForwarder(self, True, self.port_mappings, on_bytes=self._udp_bytes)
            return await self.pc.answer(sdp)
        await self.pc.setRemoteDescription(RTCSessionDescription(direct_description(sdp), "offer"))
        await asyncio.wait_for(self.pc.setLocalDescription(await self.pc.createAnswer()), 18)
        return direct_description(self.pc.localDescription.sdp)

    async def accept_answer(self, sdp):
        if self.transport == "quic":
            if not is_quic_description(sdp):
                raise ValueError("A resposta não corresponde ao transporte QUIC escolhido.")
            description = validate_description(sdp)
            self.port_mappings = normalize_ports(description.get("extensions", {}).get("ports", []))
            await self.pc.accept_answer(sdp)
            return
        if is_quic_description(sdp):
            raise ValueError("A resposta não corresponde ao transporte escolhido.")
        await self.pc.setRemoteDescription(RTCSessionDescription(direct_description(sdp), "answer"))

    async def wait_connected(self):
        try:
            if self.owner.role == "guest":
                # A decision at the end of the approval window can still need
                # ICE gathering and a signaling POST before its answer arrives.
                await asyncio.wait_for(self.answer_received.wait(), APPROVAL_TIMEOUT + 45)
            await asyncio.wait_for(self.ready.wait(), 40)
            if self.closed:
                return
        except asyncio.TimeoutError:
            self.notify("failed")
            if self.owner.role == "guest":
                self.owner.emit({"type": "error", "message":
                    ("O pedido de entrada expirou. Entre novamente para enviar outro pedido."
                     if not self.answer_received.is_set() else
                     "Não foi possível criar o caminho direto. Confira o convite, o mundo aberto e a rede.")})
                asyncio.create_task(self.owner.stop())
            await self.close()

    async def accept_tcp(self, reader, writer, mapping_id=None):
        if self.closed or len(self.streams) >= MAX_STREAMS:
            writer.close()
            return
        label = "tcp:" + uuid.uuid4().hex + (":" + mapping_id if mapping_id else "")
        channel = self.pc.createDataChannel(label, ordered=True)
        bridge = StreamBridge(self, channel, reader, writer)
        self.streams.add(bridge)
        bridge.task = asyncio.current_task()
        await bridge.run()

    async def close(self):
        if self._close_task is None:
            self.closed = True
            self._close_task = asyncio.create_task(self._close(asyncio.current_task()))
        await asyncio.shield(self._close_task)

    async def _close(self, initiator):
        for server in self.extra_servers:
            server.close()
        if self.udp:
            await self.udp.close()
        for stream in list(self.streams):
            if stream.writer:
                stream.writer.transport.abort()
            stream.close()
        self.delivery.close()
        self.send_buffer.close()
        for task in (self.ping_task, self.connect_task, self.negotiation_task):
            if task and task is not initiator and task is not asyncio.current_task():
                task.cancel()
        await self.pc.close()
        for server in self.extra_servers:
            await server.wait_closed()
        self.extra_servers.clear()
        if self.owner.peers.get(self.peer_id) is self:
            self.owner.peers.pop(self.peer_id, None)
        self.notify("disconnected")


class TunnelService:
    def __init__(self, emit: Callable[[dict], None], *, endpoint="https://ntfy.sh", stun=True,
                 identity=None, trust_store=None, transport="webrtc"):
        if transport not in ("quic", "webrtc"):
            raise ValueError("Transporte inválido. Escolha QUIC ou WebRTC.")
        self.emit = emit
        self.transport = transport
        self.extra_ports = []
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
        self.pending_requests = {}
        self.request_ids = set()
        self.room_lock = asyncio.Lock()
        self.renew_task = None
        self.replacement_bus = None
        self.retired_buses = set()
        self.waiting_approval = False
        self.stop_task = None
        self.identity = identity
        self.trust_store = trust_store if trust_store is not None else TrustStore()
        self.revoked_fingerprints = set()
        self.trust_read_failed = False

    async def _measure_loop(self):
        try:
            while True:
                start = time.perf_counter()
                await asyncio.sleep(.5)
                self.loop_lag.append(max(0, (time.perf_counter() - start - .5) * 1000))
        except asyncio.CancelledError:
            pass

    def _begin(self):
        if self.stop_task and not self.stop_task.done():
            raise ValueError("Aguarde o encerramento da conexão atual.")
        if self.bus or self.starting:
            raise ValueError("Encerre a conexão atual antes de iniciar outra.")
        self.generation += 1
        self.starting = True
        self.stopping = False
        self.stop_task = None
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

    def _active_source(self, bus, generation):
        return not self.stopping and self.bus is bus and generation == self.generation

    def _make_bus(self, code, peer_id, is_host, generation):
        async def receive(message):
            await self._signal(message, bus, generation)

        def status(message):
            if self._active_source(bus, generation):
                self.signal_status(message)

        bus = SignalBus(code, peer_id, is_host, receive, status, endpoint=self.endpoint)
        return bus

    def signal_status(self, message):
        state = "hosting" if self.role == "host" else (
            "connected" if any(p.ready.is_set() and not p.closed for p in self.peers.values()) else
            "waiting_approval" if self.waiting_approval else "connecting")
        self.emit({"type": "status", "state": state,
                   "role": self.role, "message": message})

    async def host(self, port, name="Meu mundo", ports=None):
        if self.bus or self.starting:
            raise ValueError("Encerre a sala atual antes de abrir outra.")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("Informe a porta mostrada pelo Minecraft ao abrir para LAN.")
        mappings = normalize_ports(ports or [], game_port=port)
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
        self.extra_ports = mappings
        self.stopping = False
        self.sent = self.received = 0
        self.code = new_code()
        self.signal_status("Preparando o convite…")
        bus = self._make_bus(self.code, "host", True, generation)
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
        self.emit({"type": "room", "code": self.code, "name": self.name, "port": port,
                   "ports": list(self.extra_ports)})
        self.emit({"type": "status", "state": "hosting", "role": "host", "message": "Sala aberta. Convide seus amigos."})
        self.metrics_task = asyncio.create_task(self._metrics())
        self.loop_lag_task = asyncio.create_task(self._measure_loop())
        return self.code

    async def join(self, code, port=0, name="Amigo", *, transport=None):
        if self.bus or self.starting:
            raise ValueError("Encerre a conexão atual antes de entrar em outra sala.")
        normalized = normalize_code(code)
        if not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("Porta local inválida.")
        selected = transport or self.transport
        if selected not in ("quic", "webrtc"):
            raise ValueError("Transporte inválido. Escolha QUIC ou WebRTC.")
        generation = self._begin()
        self.code, self.role, self.local_port = normalized, "guest", port
        self.extra_ports = []
        self.stopping = False
        self.sent = self.received = 0
        self.signal_status("Encontrando o mundo…")
        bus = self._make_bus(self.code, uuid.uuid4().hex, False, generation)
        self.bus = bus
        try:
            await bus.start()
            self._check_start(generation)
            peer = TunnelPeer(self, "host", "Anfitrião", transport=selected)
            self.peers["host"] = peer
            peer.notify("connecting")
            sdp = await peer.offer()
            self._check_start(generation)
            if self.identity is None:
                self.identity = Identity.load_or_create()
            guest_name = safe_name(name)
            proof = self.identity.sign_offer(self.code, bus.peer_id, sdp, guest_name)
            await bus.send("host", {"type": "offer", "sdp": sdp, "name": guest_name, "identity": proof})
            self._check_start(generation)
            self.waiting_approval = not peer.answer_received.is_set()
            if self.waiting_approval:
                self.signal_status("Pedido enviado. Aguardando aprovação do anfitrião…")
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

    async def _signal(self, message, bus=None, generation=None):
        # Never block the subscription reader on ICE gathering or TCP work.
        bus = self.bus if bus is None else bus
        generation = self.generation if generation is None else generation
        if self._active_source(bus, generation):
            self.spawn(self._handle_signal(message, bus, generation))

    def _admission(self, peer_id, request, state):
        self.emit({"type": "admission", "peer_id": peer_id,
                   "name": request["name"], "state": state,
                   "fingerprint": request.get("fingerprint", ""),
                   "can_trust": bool(request.get("public_key")),
                   "trusted": self._is_trusted(request.get("public_key"), request.get("fingerprint"))})

    def _take_request(self, peer_id, state):
        request = self.pending_requests.pop(peer_id)
        expiry = request.get("expiry")
        if expiry and expiry is not asyncio.current_task():
            expiry.cancel()
        self._admission(peer_id, request, state)
        return request

    def _require_host(self):
        if self.role != "host" or self.stopping or not self.bus or self.starting:
            raise ValueError("Abra uma sala antes de usar este controle.")

    def _require_room(self, bus, generation):
        self._require_host()
        if not self._active_source(bus, generation):
            raise ValueError("A sala ou o convite mudou. Use os controles da sala atual.")

    async def _decline(self, bus, generation, peer_id, message):
        if self._active_source(bus, generation):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(bus.send(peer_id, {"type": "error", "message": message}), 2)

    async def _expire_request(self, peer_id, request, bus, generation):
        await asyncio.sleep(max(0, request["expires_at"] - time.monotonic()))
        if not self._active_source(bus, generation) or self.pending_requests.get(peer_id) is not request:
            return
        self._take_request(peer_id, "expired")
        await self._decline(bus, generation, peer_id, "O pedido de entrada expirou. Entre novamente.")

    async def trusted_computers(self):
        computers = self._trusted_list()
        self.emit({"type": "trusted", "computers": computers})
        return computers

    def _is_trusted(self, public_key, fingerprint):
        if not public_key or fingerprint in self.revoked_fingerprints:
            return False
        try:
            trusted = self.trust_store.is_trusted(public_key)
        except (OSError, ValueError):
            self._trust_read_failure()
            return False
        self.trust_read_failed = False
        return trusted

    def _trust_read_failure(self):
        if not self.trust_read_failed:
            self.trust_read_failed = True
            self.emit({"type": "error", "operation": "trust", "message":
                       "Não foi possível ler os computadores confiáveis. Novas entradas vão aguardar aprovação."})

    def _trusted_list(self):
        return [item for item in self.trust_store.list()
                if item["fingerprint"] not in self.revoked_fingerprints]

    def _trust_changed(self, public_key=None):
        for peer in list(self.peers.values()):
            if peer.public_key and (public_key is None or peer.public_key == public_key):
                peer.notify("connected" if peer.ready.is_set() else "connecting")
        for peer_id, request in list(self.pending_requests.items()):
            if request.get("public_key") and (public_key is None or request["public_key"] == public_key):
                self._admission(peer_id, request, "pending")
        try:
            computers = self._trusted_list()
        except (OSError, ValueError):
            self._trust_read_failure()
            computers = []
        self.emit({"type": "trusted", "computers": computers})

    async def set_trusted(self, peer_id, trusted):
        if not isinstance(trusted, bool):
            raise ValueError("Escolha se deseja confiar neste computador.")
        bus, generation = self.bus, self.generation
        async with self.room_lock:
            self._require_room(bus, generation)
            peer = self.peers.get(peer_id)
            if not peer or peer.closed or not peer.ready.is_set():
                raise ValueError("Este participante já saiu ou ainda está conectando.")
            if not peer.public_key:
                raise ValueError("Este computador precisa atualizar o Ponte para permitir confiança.")
            if trusted:
                self.trust_store.remember(peer.public_key, peer.name, timeout=0)
                self.revoked_fingerprints.discard(peer.fingerprint)
            else:
                self.revoked_fingerprints.add(peer.fingerprint)
                try:
                    self.trust_store.forget(peer.fingerprint, timeout=0)
                finally:
                    self._trust_changed(peer.public_key)
                return
            self._trust_changed(peer.public_key)

    async def forget_trusted(self, fingerprint):
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("Identificação do computador inválida.")
        self.revoked_fingerprints.add(fingerprint)
        try:
            self.trust_store.forget(fingerprint, timeout=0)
        finally:
            self._trust_changed()

    async def approve(self, peer_id, remember=False):
        return await self._approve(peer_id, remember)

    async def _approve(self, peer_id, remember=False, *, automatic=False):
        bus, generation = self.bus, self.generation
        async with self.room_lock:
            self._require_room(bus, generation)
            request = self.pending_requests.get(peer_id)
            if request is None:
                raise ValueError("Este pedido já foi encerrado. Aguarde um novo pedido de entrada.")
            if automatic and not self._is_trusted(request.get("public_key"), request.get("fingerprint")):
                return
            if request["expires_at"] <= time.monotonic():
                self._take_request(peer_id, "expired")
                self.spawn(self._decline(bus, generation, peer_id, "O pedido de entrada expirou. Entre novamente."))
                raise ValueError("Este pedido expirou. A pessoa precisa entrar novamente.")
            if len(self.peers) >= MAX_PEERS:
                raise ValueError("A sala já tem oito participantes. Remova alguém antes de aprovar.")
            if remember:
                if not request.get("public_key"):
                    raise ValueError("Este computador precisa atualizar o Ponte para permitir confiança.")
                self.trust_store.remember(request["public_key"], request["name"], timeout=0)
                self.revoked_fingerprints.discard(request["fingerprint"])
            self._take_request(peer_id, "approved")
            peer = TunnelPeer(self, peer_id, request["name"],
                              transport="quic" if is_quic_description(request["sdp"]) else "webrtc")
            peer.public_key = request.get("public_key")
            peer.fingerprint = request.get("fingerprint", "")
            self.peers[peer_id] = peer
            peer.notify("connecting")
            if remember:
                self._trust_changed(peer.public_key)
            peer.negotiation_task = asyncio.current_task()
        try:
            sdp = await peer.answer(request["sdp"])
            if not self._active_source(bus, generation) or self.peers.get(peer_id) is not peer:
                await peer.close()
                return
            await bus.send(peer_id, {"type": "answer", "sdp": sdp, "name": self.name})
            if not self._active_source(bus, generation) or peer.closed:
                await peer.close()
                return
            peer.connect_task = asyncio.create_task(peer.wait_connected())
        except BaseException:
            await peer.close()
            raise
        finally:
            peer.negotiation_task = None

    async def _approve_trusted(self, peer_id, request, bus, generation):
        if not self._active_source(bus, generation) or self.pending_requests.get(peer_id) is not request:
            return
        try:
            await self._approve(peer_id, automatic=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._active_source(bus, generation):
                self.emit({"type": "error", "operation": "peer", "peer_id": peer_id,
                           "message": "Não foi possível concluir a entrada do computador confiável. Tente novamente."})

    async def reject(self, peer_id):
        bus, generation = self.bus, self.generation
        async with self.room_lock:
            self._require_room(bus, generation)
            if peer_id not in self.pending_requests:
                raise ValueError("Este pedido já foi encerrado. Aguarde um novo pedido de entrada.")
            self._take_request(peer_id, "rejected")
        await self._decline(bus, generation, peer_id, "O anfitrião recusou o pedido de entrada.")

    async def remove_peer(self, peer_id):
        bus, generation = self.bus, self.generation
        trust_error = None
        async with self.room_lock:
            self._require_room(bus, generation)
            peer = self.peers.get(peer_id)
            if peer is None or peer.closed:
                raise ValueError("Este participante já saiu da sala.")
            if peer.public_key:
                self.revoked_fingerprints.add(peer.fingerprint)
                try:
                    self.trust_store.forget(peer.fingerprint, timeout=0)
                except (OSError, ValueError) as exc:
                    trust_error = exc
            self.peers.pop(peer_id)
        await peer.close()
        if peer.public_key:
            self._trust_changed(peer.public_key)
        if trust_error:
            self.emit({"type": "error", "operation": "trust", "peer_id": peer_id,
                       "fingerprint": peer.fingerprint, "message":
                       "Participante removido e confiança revogada nesta execução. Não foi possível salvar a revogação no disco."})
        # RTC closure revokes the direct path immediately, even after renewal
        # has retired the rendezvous used by this participant.
        if self._active_source(bus, generation):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(bus.send(peer_id, {"type": "bye", "message":
                    "O anfitrião removeu você da sala."}), 1)

    async def renew_invite(self):
        old_bus, generation = self.bus, self.generation
        async with self.room_lock:
            self._require_room(old_bus, generation)
            if self.renew_task:
                raise ValueError("O convite já está sendo renovado.")
            self.renew_task = asyncio.current_task()
            self.emit({"type": "room_action", "state": "renewing", "message": "Renovando convite…"})
        candidate = None
        try:
            candidate = self._make_bus(new_code(), "host", True, generation + 1)
            self.replacement_bus = candidate
            await candidate.start()
            async with self.room_lock:
                if not self._active_source(old_bus, generation):
                    raise asyncio.CancelledError()
                self.generation += 1
                self.retired_buses.add(old_bus)
                self.bus, self.code = candidate, candidate.code
                self.replacement_bus = None
                cancelled = list(self.pending_requests)
                for peer_id in list(self.pending_requests):
                    self._take_request(peer_id, "cancelled")
                self.request_ids.clear()
                unfinished = []
                for peer in list(self.peers.values()):
                    if not peer.ready.is_set():
                        cancelled.append(peer.peer_id)
                        self.peers.pop(peer.peer_id)
                        unfinished.append(peer)
            await asyncio.gather(*(peer.close() for peer in unfinished))
            await asyncio.gather(*(asyncio.wait_for(old_bus.send(peer_id, {"type": "error", "message":
                "O convite foi renovado. Peça o novo convite ao anfitrião."}), .5)
                for peer_id in cancelled), return_exceptions=True)
            await old_bus.close()
            self.retired_buses.discard(old_bus)
            self._check_start(generation + 1)
            self.emit({"type": "room", "code": self.code, "name": self.name,
                       "port": self.host_port, "ports": list(self.extra_ports), "renewed": True})
            return self.code
        except BaseException:
            if candidate and self.bus is not candidate:
                await candidate.close()
            raise
        finally:
            if self.replacement_bus is candidate:
                self.replacement_bus = None
            if self.renew_task is asyncio.current_task():
                self.renew_task = None
                self.emit({"type": "room_action", "state": "ready", "message": ""})

    async def _handle_signal(self, message, bus=None, generation=None):
        bus = self.bus if bus is None else bus
        generation = self.generation if generation is None else generation
        if not self._active_source(bus, generation):
            return
        sender, kind = message.get("_sender"), message.get("type")
        peer = None
        try:
            if self.role == "host" and kind == "offer" and sender != "host":
                async with self.room_lock:
                    if not self._active_source(bus, generation) or sender in self.peers or sender in self.request_ids:
                        return
                    if not isinstance(sender, str) or not re.fullmatch(r"[0-9a-f]{32}", sender):
                        return
                    if len(self.request_ids) >= MAX_REQUEST_IDS:
                        self.spawn(self._decline(bus, generation, sender, "Renove o convite para receber novos pedidos."))
                        return
                    # Keep bounded tombstones for the invitation lifetime: a
                    # duplicate or rejected sender must explicitly join anew.
                    self.request_ids.add(sender)
                    if len(self.pending_requests) >= MAX_PENDING or len(self.peers) >= MAX_PEERS:
                        self.spawn(self._decline(bus, generation, sender, "A sala está sem vagas para novos pedidos. Tente novamente depois."))
                        return
                    sdp = admission_description(message.get("sdp"))
                    identity = (verify_offer(self.code, sender, sdp, message.get("name", "Amigo"),
                                             message["identity"]) if "identity" in message else {})
                    request = {"name": safe_name(message.get("name", "Amigo")), "sdp": sdp,
                               **identity,
                               "expires_at": time.monotonic() + APPROVAL_TIMEOUT}
                    self.pending_requests[sender] = request
                    request["expiry"] = self.spawn(self._expire_request(sender, request, bus, generation))
                    self._admission(sender, request, "pending")
                await bus.send(sender, {"type": "hello", "state": "pending"})
                if (self._active_source(bus, generation) and request.get("public_key")
                        and self._is_trusted(request["public_key"], request.get("fingerprint"))):
                    self.spawn(self._approve_trusted(sender, request, bus, generation))
            elif self.role == "guest" and sender == "host" and kind == "answer":
                peer = self.peers.get("host")
                if peer and peer.pc.signalingState == "have-local-offer":
                    peer.name = safe_name(message.get("name", "Anfitrião"))
                    await peer.accept_answer(message.get("sdp"))
                    if self._active_source(bus, generation) and not peer.closed:
                        peer.answer_received.set()
                        self.waiting_approval = False
                        self.signal_status("Entrada aprovada. Criando conexão direta…")
            elif self.role == "guest" and sender == "host" and kind == "hello" and message.get("state") == "pending":
                peer = self.peers.get("host")
                if peer and not peer.answer_received.is_set():
                    self.waiting_approval = True
                    self.signal_status("Aguardando aprovação do anfitrião…")
            elif self.role == "host" and kind == "bye" and sender in self.pending_requests:
                async with self.room_lock:
                    if self._active_source(bus, generation) and sender in self.pending_requests:
                        self._take_request(sender, "cancelled")
            elif kind == "bye" and sender in self.peers:
                peer = self.peers[sender]
                await peer.close()
                if self.role == "guest":
                    self.emit({"type": "error", "message": safe_name(message.get("message", "O anfitrião encerrou a sala."))})
                    asyncio.create_task(self.stop())
            elif self.role == "guest" and sender == "host" and kind == "error":
                self.emit({"type": "error", "message": safe_name(message.get("message", "Não foi possível entrar na sala."))})
                asyncio.create_task(self.stop())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("Negociação interrompida: %s", type(exc).__name__)
            if not self._active_source(bus, generation):
                return
            if peer and self.peers.get(sender) is peer:
                await peer.close()
            self.emit({"type": "error", "message": "Não foi possível negociar a conexão direta. Tente entrar novamente.",
                       "operation": "peer" if self.role == "host" else "connection"})
            if self.role == "guest":
                asyncio.create_task(self.stop())

    async def _listen(self, peer):
        if self.stopping or peer.closed:
            return
        try:
            server = await asyncio.start_server(peer.accept_tcp, "127.0.0.1", self.local_port,
                                                start_serving=False)
            if self.stopping or peer.closed:
                server.close()
                await server.wait_closed()
                return
            self.server = server
            await server.start_serving()
            self.local_port = self.server.sockets[0].getsockname()[1]
            extra = []
            for mapping in peer.port_mappings:
                if mapping["protocol"] != "tcp":
                    continue
                async def accept(reader, writer, mapping_id=mapping["id"]):
                    await peer.accept_tcp(reader, writer, mapping_id)
                listener = await asyncio.start_server(accept, "127.0.0.1", mapping["local_port"],
                                                      start_serving=False)
                peer.extra_servers.append(listener)
                if self.stopping or peer.closed:
                    listener.close()
                    raise asyncio.CancelledError()
                await listener.start_serving()
                bound_port = listener.sockets[0].getsockname()[1]
                extra.append({**mapping, "local_port": bound_port,
                              "local_address": f"127.0.0.1:{bound_port}"})
            if peer.transport == "quic":
                peer.udp = UdpForwarder(peer, False, peer.port_mappings, on_bytes=peer._udp_bytes)
                extra.extend(await peer.udp.start_guest())
            if self.stopping or peer.closed:
                return
            for record in extra:
                record["local_address"] = f"127.0.0.1:{record['local_port']}"
            self.emit({"type": "listening", "address": f"127.0.0.1:{self.local_port}",
                       "ports": extra, "transport": peer.transport})
            self.emit({"type": "status", "state": "connected", "role": "guest",
                       "message": "Conexão direta pronta. Entre pelo Minecraft."})
        except OSError:
            self.emit({"type": "error", "message": "Uma porta local está ocupada. Ajuste as portas adicionais ou encerre o programa que a utiliza."})
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
        if self.stop_task is None:
            self.stop_task = asyncio.create_task(self._stop())
        await asyncio.shield(self.stop_task)

    async def _stop(self):
        if self.stopping:
            return
        self.generation += 1
        self.starting = False
        self.stopping = True
        renewing = self.renew_task
        if renewing and renewing is not asyncio.current_task():
            # Once committed, let the short RTC/retired-bus cleanup finish;
            # cancelling it midway could interrupt RTCPeerConnection.close().
            if self.replacement_bus is not None:
                renewing.cancel()
            await asyncio.gather(renewing, return_exceptions=True)
        replacement, self.replacement_bus = self.replacement_bus, None
        if replacement:
            await replacement.close()
        for retired in list(self.retired_buses):
            await retired.close()
            self.retired_buses.discard(retired)
        server, self.server = self.server, None
        if server:
            server.close()
        for task in list(self.tasks):
            if task is not asyncio.current_task():
                task.cancel()
        await asyncio.gather(*(t for t in list(self.tasks) if t is not asyncio.current_task()), return_exceptions=True)
        for peer_id in list(self.pending_requests):
            self._take_request(peer_id, "cancelled")
        self.request_ids.clear()
        # Data-channel closure is immediate. A bye publish is best-effort only.
        for peer in list(self.peers.values()):
            await peer.close()
        self.peers.clear()
        if server:
            # Python 3.12 also waits for accepted transports here. Close the
            # peers first so a game socket cannot keep shutdown waiting.
            await server.wait_closed()
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
            if self.role == "guest" and self.waiting_approval:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(bus.send("host", {"type": "bye"}), .3)
            await bus.close()
        self.code = ""
        self.waiting_approval = False
        self.emit({"type": "status", "state": "stopped", "message": "Conexão encerrada.", "role": self.role})

    def diagnostics(self):
        from . import __version__
        return {"version": __version__, "role": self.role, "active": bool(self.bus),
                "sent": self.sent, "received": self.received,
                "event_loop_lag": summary(self.loop_lag),
                "peers": [{"state": p.pc.connectionState, "route": p.route(), "rtt_ms": p.latency.latest(),
                           "transport": p.transport,
                           "latency": p.latency.diagnostics(), "streams": len(p.streams),
                           "delivery": p.delivery.diagnostics(),
                           "quic": p.pc.diagnostics() if p.transport == "quic" else None,
                           "udp": p.udp.diagnostics() if p.udp else None,
                           "buffered_bytes": sum(s.channel.bufferedAmount for s in p.streams),
                           "receive_queued_bytes": sum(s.queued_bytes for s in p.streams),
                           "send_unacked_bytes": sum(WINDOW - s.credit for s in p.streams)} for p in self.peers.values()],
                "transport": "QUIC ou WebRTC / ICE / STUN; sem TURN", "signaling": self.endpoint}
