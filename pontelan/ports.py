"""Host-authorized extra ports and bounded, unreliable UDP over QUIC DATAGRAM.

Only mapping ordinals and opaque association IDs cross the wire. The host's
advertised mappings are the sole source of loopback destination ports.
"""
from __future__ import annotations

import asyncio
import math
import re
import secrets
import struct
import time
from dataclasses import dataclass, field

MAX_PORTS = 8
MAX_DATAGRAM = 1000
MAX_UDP_PAYLOAD = 65507
HEADER = struct.Struct("!2sBBIIHBB")
FRAGMENT_PAYLOAD = MAX_DATAGRAM - HEADER.size
MAX_FRAGMENTS = math.ceil(MAX_UDP_PAYLOAD / FRAGMENT_PAYLOAD)
MAX_ASSOCIATIONS = 64
MAX_PENDING_TASKS = 64
MAX_BUFFER_BYTES = 262144
MAX_REASSEMBLIES = 256
FRAGMENT_TTL = 0.5
ASSOCIATION_TTL = 30.0
MAGIC = b"PU"
WIRE_VERSION = 1


def normalize_ports(raw, game_port=None):
    """Validate local configuration or an authenticated host advertisement.

    Missing IDs receive stable positional IDs p1..p8. A zero local port asks
    the OS for a free guest port; omitted local ports use the host port.
    """
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_PORTS:
        raise ValueError("Configure no máximo 8 portas extras.")
    result, ids, local_ports = [], set(), set()
    for index, entry in enumerate(raw, 1):
        if not isinstance(entry, dict) or set(entry) - {
            "id", "name", "protocol", "host_port", "local_port"
        }:
            raise ValueError("Configuração de porta extra inválida.")
        mapping_id = entry.get("id", f"p{index}")
        name = entry.get("name", "")
        protocol = entry.get("protocol")
        host_port = entry.get("host_port")
        local_port = entry.get("local_port", host_port)
        if (not isinstance(mapping_id, str)
                or not re.fullmatch(r"p[1-8]", mapping_id)
                or mapping_id in ids):
            raise ValueError("Identificador de porta extra inválido ou repetido.")
        if (not isinstance(name, str) or len(name) > 40
                or any(not char.isprintable() for char in name)):
            raise ValueError("O nome da porta deve ter até 40 caracteres.")
        if protocol not in ("tcp", "udp"):
            raise ValueError("O protocolo deve ser TCP ou UDP.")
        if type(host_port) is not int or not 1 <= host_port <= 65535:
            raise ValueError("A porta do anfitrião deve estar entre 1 e 65535.")
        if type(local_port) is not int or not 0 <= local_port <= 65535:
            raise ValueError("A porta local deve estar entre 0 e 65535.")
        if protocol == "tcp" and game_port is not None and host_port == game_port:
            raise ValueError("A porta TCP do jogo já é encaminhada pela sala.")
        if local_port and (protocol, local_port) in local_ports:
            raise ValueError("Há portas locais repetidas para o mesmo protocolo.")
        ids.add(mapping_id)
        if local_port:
            local_ports.add((protocol, local_port))
        result.append({"id": mapping_id, "name": name.strip(), "protocol": protocol,
                       "host_port": host_port, "local_port": local_port})
    return result


def _send_udp(transport, payload, addr=None):
    """DatagramTransport skips b''; send that valid packet via a short-lived dup."""
    try:
        if payload:
            transport.sendto(payload, addr)
        else:
            # The transport keeps ownership of its socket. The duplicate is
            # nonblocking and closed immediately after this uncommon send.
            with transport.get_extra_info("socket").dup() as udp_socket:
                udp_socket.setblocking(False)
                if addr is None:
                    udp_socket.send(payload)
                else:
                    udp_socket.sendto(payload, addr)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


@dataclass
class _Association:
    mapping: int
    ident: int
    touched: float
    source: tuple | None = None
    transport: asyncio.DatagramTransport | None = None
    opened: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _Packet:
    total: int
    count: int
    deadline: float
    chunks: dict = field(default_factory=dict)
    size: int = 0


class _LocalProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner, mapping):
        self.owner, self.mapping = owner, mapping

    def datagram_received(self, data, addr):
        self.owner._local_packet(self.mapping, data, addr)


class _DestinationProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner, association):
        self.owner, self.association = owner, association

    def datagram_received(self, data, addr):
        self.owner._destination_packet(self.association, data)


class UdpForwarder:
    """One authenticated peer's UDP associations; never retransmit packets.

    ``peer.ready`` is an asyncio.Event, ``peer.closed`` a bool, and
    ``peer.send_datagram(bytes)`` returns False when congested. ``ready`` can
    override the default readiness check. Register ``handle_datagram`` only
    after the authenticated host mappings have been installed. ``on_bytes``
    receives payload totals once per complete packet, excluding wire headers.
    """
    def __init__(self, peer, is_host, mappings, emit=None, clock=None, ready=None,
                 on_bytes=None):
        self.peer, self.is_host = peer, bool(is_host)
        self.mappings = normalize_ports(mappings)
        self.emit = emit
        self.on_bytes = on_bytes
        self.clock = clock or time.monotonic
        self._ready_override = ready
        self.closed = False
        self._listeners = {}
        self._associations = {}
        self._sources = {}
        self._packets = {}
        self._seen = {}
        self._buffer_bytes = 0
        self._pending_bytes = 0
        self._tasks = set()
        self._reaper = None
        self._packet_id = secrets.randbits(32)
        self._stats = dict(sent_packets=0, received_packets=0, sent_bytes=0,
                           received_bytes=0, dropped_packets=0, invalid_frames=0,
                           fragment_timeouts=0, association_timeouts=0)

    def _ready(self):
        if self.closed or getattr(self.peer, "closed", False):
            return False
        if self._ready_override is not None:
            return bool(self._ready_override())
        ready = getattr(self.peer, "ready", None)
        return bool(ready is not None and ready.is_set())

    def _ensure_reaper(self):
        if not self.closed and self._reaper is None:
            self._reaper = asyncio.create_task(self._reap())

    async def start_guest(self):
        """Atomically bind the guest's UDP ports to IPv4 loopback only."""
        if self.is_host:
            raise ValueError("Somente o convidado abre portas locais.")
        if self.closed or not self._ready():
            raise RuntimeError("A conexão ainda não está pronta.")
        if self._listeners:
            return self._listener_records()
        loop = asyncio.get_running_loop()
        opened = {}
        try:
            for index, mapping in enumerate(self.mappings):
                if mapping["protocol"] != "udp":
                    continue
                transport, _ = await loop.create_datagram_endpoint(
                    lambda index=index: _LocalProtocol(self, index),
                    local_addr=("127.0.0.1", mapping["local_port"]),
                )
                opened[index] = transport
                if self.closed or not self._ready():
                    raise RuntimeError("A conexão foi encerrada.")
        except BaseException:
            for transport in opened.values():
                transport.close()
            # DatagramTransport releases its socket on the next loop turn.
            await asyncio.sleep(0)
            raise
        self._listeners = opened
        self._ensure_reaper()
        records = self._listener_records()
        if self.emit is not None:
            self.emit({"type": "udp_listeners", "ports": records})
        return records

    def _listener_records(self):
        return [dict(self.mappings[index], local_port=transport.get_extra_info("sockname")[1])
                for index, transport in self._listeners.items()]

    def _schedule(self, coroutine, size):
        if (not self._ready() or len(self._tasks) >= MAX_PENDING_TASKS
                or self._pending_bytes + size > MAX_BUFFER_BYTES):
            coroutine.close()
            self._stats["dropped_packets"] += 1
            return False
        self._pending_bytes += size

        async def run():
            try:
                await coroutine
            except (OSError, ConnectionError, RuntimeError):
                self._stats["dropped_packets"] += 1

        def finished(task):
            self._tasks.discard(task)
            self._pending_bytes -= size
            # A task cancelled before its first loop turn never awaits the
            # nested coroutine. Explicit close also covers that path.
            coroutine.close()

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(finished)
        return True

    def _local_packet(self, mapping, data, addr):
        if not self._ready() or self.is_host or mapping not in self._listeners:
            return
        if len(data) > MAX_UDP_PAYLOAD or addr[0] != "127.0.0.1":
            self._stats["dropped_packets"] += 1
            return
        self._expire()
        key = (mapping, addr)
        ident = self._sources.get(key)
        association = self._associations.get(ident)
        if association is None:
            if len(self._associations) >= MAX_ASSOCIATIONS:
                self._stats["dropped_packets"] += 1
                return
            ident = secrets.randbits(32)
            while not ident or ident in self._associations:
                ident = secrets.randbits(32)
            association = _Association(mapping, ident, self.clock(), source=addr)
            self._associations[ident] = association
            self._sources[key] = ident
        association.touched = self.clock()
        self._schedule(self._send_packet(association, data), len(data))

    def _destination_packet(self, association, data):
        if (not self._ready() or not self.is_host or len(data) > MAX_UDP_PAYLOAD
                or self._associations.get(association.ident) is not association):
            return
        association.touched = self.clock()
        self._schedule(self._send_packet(association, data), len(data))

    async def _send_packet(self, association, data):
        self._packet_id = (self._packet_id + 1) & 0xFFFFFFFF
        packet_id = self._packet_id
        count = max(1, math.ceil(len(data) / FRAGMENT_PAYLOAD))
        for index in range(count):
            if (not self._ready()
                    or self._associations.get(association.ident) is not association):
                self._stats["dropped_packets"] += 1
                return
            frame = HEADER.pack(MAGIC, WIRE_VERSION, association.mapping, association.ident,
                                packet_id, len(data), index, count)
            frame += data[index * FRAGMENT_PAYLOAD:(index + 1) * FRAGMENT_PAYLOAD]
            if not self.peer.send_datagram(frame):
                self._stats["dropped_packets"] += 1
                return
            # Let QUIC flush its bounded queue before offering another frame.
            # A large synchronous batch would exhaust an idle queue even when
            # the congestion window has room for the entire UDP packet.
            await asyncio.sleep(0)
        if (not self._ready()
                or self._associations.get(association.ident) is not association):
            self._stats["dropped_packets"] += 1
            return
        self._stats["sent_packets"] += 1
        self._stats["sent_bytes"] += len(data)
        if self.on_bytes is not None:
            self.on_bytes(sent=len(data), received=0)

    def handle_datagram(self, data):
        """Validate a bounded frame, then dispatch only complete UDP packets."""
        if not self._ready():
            return
        self._ensure_reaper()
        self._expire()
        if not isinstance(data, bytes) or not HEADER.size <= len(data) <= MAX_DATAGRAM:
            self._stats["invalid_frames"] += 1
            return
        magic, version, mapping, ident, packet_id, total, index, count = HEADER.unpack_from(data)
        size = len(data) - HEADER.size
        expected_count = max(1, math.ceil(total / FRAGMENT_PAYLOAD))
        if (magic != MAGIC or version != WIRE_VERSION or not ident
                or mapping >= len(self.mappings) or self.mappings[mapping]["protocol"] != "udp"
                or total > MAX_UDP_PAYLOAD or count != expected_count
                or count > MAX_FRAGMENTS or index >= count
                or size != min(FRAGMENT_PAYLOAD, max(0, total - index * FRAGMENT_PAYLOAD))):
            self._stats["invalid_frames"] += 1
            return
        association = self._associations.get(ident)
        if ((association is not None and association.mapping != mapping)
                or (not self.is_host and association is None)):
            self._stats["invalid_frames"] += 1
            return
        key = (mapping, ident, packet_id)
        if key in self._seen:
            self._stats["dropped_packets"] += 1
            return
        packet = self._packets.get(key)
        if packet is not None and (packet.total != total or packet.count != count):
            self._forget_packet(key)
            self._remember(key)
            self._stats["invalid_frames"] += 1
            return
        if packet is None:
            if len(self._packets) >= MAX_REASSEMBLIES:
                self._stats["dropped_packets"] += 1
                return
            packet = _Packet(total, count, self.clock() + FRAGMENT_TTL)
            self._packets[key] = packet
        if index in packet.chunks:
            self._stats["dropped_packets"] += 1
            return
        if self._buffer_bytes + size > MAX_BUFFER_BYTES:
            self._forget_packet(key)
            self._remember(key)
            self._stats["dropped_packets"] += 1
            return
        packet.chunks[index] = data[HEADER.size:]
        packet.size += size
        self._buffer_bytes += size
        if len(packet.chunks) != count:
            return
        payload = b"".join(packet.chunks[part] for part in range(count))
        self._forget_packet(key)
        self._remember(key)
        self._stats["received_packets"] += 1
        self._stats["received_bytes"] += total
        if self.on_bytes is not None:
            self.on_bytes(sent=0, received=total)
        if self.is_host:
            self._schedule(self._host_deliver(mapping, ident, payload), len(payload))
        else:
            transport = self._listeners.get(mapping)
            if transport is not None and association.source is not None:
                association.touched = self.clock()
                if not _send_udp(transport, payload, association.source):
                    self._stats["dropped_packets"] += 1

    async def _host_deliver(self, mapping, ident, payload):
        if not self._ready():
            return
        association = self._associations.get(ident)
        if association is None:
            if len(self._associations) >= MAX_ASSOCIATIONS:
                self._stats["dropped_packets"] += 1
                return
            association = _Association(mapping, ident, self.clock())
            self._associations[ident] = association
            try:
                transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                    lambda: _DestinationProtocol(self, association),
                    remote_addr=("127.0.0.1", self.mappings[mapping]["host_port"]),
                )
                if not self._ready() or self._associations.get(ident) is not association:
                    transport.close()
                    return
                association.transport = transport
            except BaseException:
                if self._associations.get(ident) is association:
                    self._associations.pop(ident, None)
                raise
            finally:
                association.opened.set()
        else:
            await association.opened.wait()
        if (self._ready() and association.mapping == mapping and association.transport is not None
                and self._associations.get(ident) is association):
            association.touched = self.clock()
            if not _send_udp(association.transport, payload):
                self._stats["dropped_packets"] += 1

    def _forget_packet(self, key):
        packet = self._packets.pop(key, None)
        if packet is not None:
            self._buffer_bytes -= packet.size

    def _remember(self, key):
        # Bounded tombstones reject duplicates and late fragments without
        # creating a permanent packet history or keeping UDP payloads.
        if len(self._seen) >= 512:
            self._seen.pop(next(iter(self._seen)))
        self._seen[key] = self.clock() + FRAGMENT_TTL

    def _expire(self):
        now = self.clock()
        for key, packet in list(self._packets.items()):
            if packet.deadline <= now:
                self._forget_packet(key)
                self._remember(key)
                self._stats["fragment_timeouts"] += 1
        for key, expiry in list(self._seen.items()):
            if expiry <= now:
                self._seen.pop(key, None)
        for ident, association in list(self._associations.items()):
            if now - association.touched >= ASSOCIATION_TTL:
                self._associations.pop(ident, None)
                if association.source is not None:
                    self._sources.pop((association.mapping, association.source), None)
                if association.transport is not None:
                    association.transport.close()
                association.opened.set()
                self._stats["association_timeouts"] += 1

    async def _reap(self):
        try:
            while not self.closed:
                await asyncio.sleep(FRAGMENT_TTL / 2)
                self._expire()
        except asyncio.CancelledError:
            pass

    def diagnostics(self):
        return dict(self._stats, active_associations=len(self._associations),
                    reassembly_packets=len(self._packets), reassembly_bytes=self._buffer_bytes,
                    pending_tasks=len(self._tasks), pending_bytes=self._pending_bytes,
                    local_listeners=len(self._listeners))

    async def close(self):
        if self.closed:
            return
        self.closed = True
        for task in self._tasks:
            task.cancel()
        if self._reaper is not None:
            self._reaper.cancel()
        for transport in self._listeners.values():
            transport.close()
        for association in self._associations.values():
            if association.transport is not None:
                association.transport.close()
            association.opened.set()
        tasks = list(self._tasks)
        if self._reaper is not None:
            tasks.append(self._reaper)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._listeners.clear()
        self._associations.clear()
        self._sources.clear()
        self._packets.clear()
        self._seen.clear()
        self._buffer_bytes = 0
        await asyncio.sleep(0)
