import asyncio
import hashlib
import os
import random
import socket
import unittest
from unittest.mock import patch

from pontelan.event_loop import NetworkEventLoop
from pontelan.ports import (
    ASSOCIATION_TTL, FRAGMENT_PAYLOAD, FRAGMENT_TTL, HEADER, MAGIC,
    MAX_ASSOCIATIONS, MAX_BUFFER_BYTES, MAX_DATAGRAM, MAX_REASSEMBLIES,
    UdpForwarder, WIRE_VERSION, normalize_ports,
)


def mapping(port=24454, **values):
    return dict({"name": "Voice", "protocol": "udp", "host_port": port,
                 "local_port": 0}, **values)


class NormalizeTests(unittest.TestCase):
    def test_defaults_and_protocol_independence(self):
        self.assertEqual(normalize_ports(None), [])
        rows = normalize_ports([
            {"name": "UDP", "protocol": "udp", "host_port": 25565},
            {"name": "TCP", "protocol": "tcp", "host_port": 25566, "local_port": 25565},
        ], game_port=25565)
        self.assertEqual(rows[0]["id"], "p1")
        self.assertEqual(rows[1]["id"], "p2")
        self.assertEqual(rows[0]["local_port"], 25565)
        self.assertEqual(normalize_ports([mapping(id="p8")])[0]["id"], "p8")

    def test_reject_invalid_rows(self):
        invalid = [
            "udp", [None], [mapping()] * 9,
            [mapping(host_port=True)], [mapping(host_port="24454")],
            [mapping(host_port=0)], [mapping(host_port=65536)],
            [mapping(local_port=True)], [mapping(local_port=-1)],
            [mapping(local_port=65536)], [mapping(protocol="UDP")],
            [mapping(name="x" * 41)], [mapping(name="x\ny")],
            [mapping(id="p9")], [mapping(id="../p1")],
            [mapping(host_ip="192.168.1.1")],
            [mapping(id="p1"), mapping(24455, id="p1")],
            [mapping(local_port=23000), mapping(24455, local_port=23000)],
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_ports(value)
        with self.assertRaises(ValueError):
            normalize_ports([mapping(25565, protocol="tcp")], game_port=25565)


class _Peer:
    def __init__(self):
        self.ready = asyncio.Event()
        self.ready.set()
        self.closed = False
        self.remote = None
        self.frames = []
        self.drop = lambda data: False
        self.congested = False
        self.pending_limit = None
        self.pending = 0
        self.peak_pending = 0

    def send_datagram(self, data):
        if self.congested or (self.pending_limit is not None and self.pending >= self.pending_limit):
            return False
        assert len(data) <= MAX_DATAGRAM
        if self.pending_limit is not None:
            self.pending += 1
            self.peak_pending = max(self.peak_pending, self.pending)
            asyncio.get_running_loop().call_soon(self._flushed)
        self.frames.append(data)
        if not self.drop(data):
            asyncio.get_running_loop().call_soon(self.remote.handle_datagram, data)
        return True

    def _flushed(self):
        self.pending -= 1


class _Echo(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport = None
        self.received = asyncio.Queue()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.received.put_nowait((data, addr))
        self.transport.sendto(data, addr)


class _Client(asyncio.DatagramProtocol):
    def __init__(self):
        self.received = asyncio.Queue()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.received.put_nowait(data)


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class UdpTests(unittest.IsolatedAsyncioTestCase):
    def _setupAsyncioRunner(self):
        # Match the application's selector loop; Windows Proactor itself drops
        # empty UDP datagrams before calling DatagramProtocol.datagram_received.
        self._asyncioRunner = asyncio.Runner(debug=True, loop_factory=NetworkEventLoop)

    async def asyncSetUp(self):
        self.resources = []
        self.forwarders = []
        self.loop = asyncio.get_running_loop()
        transport, self.echo = await self.loop.create_datagram_endpoint(
            _Echo, local_addr=("127.0.0.1", 0))
        self.resources.append(transport)
        self.host_port = transport.get_extra_info("sockname")[1]

    async def asyncTearDown(self):
        for forwarder in self.forwarders:
            await forwarder.close()
        for transport in self.resources:
            transport.close()
        await asyncio.sleep(0)

    async def pair(self, rows=None, clock=None, host_on_bytes=None, guest_on_bytes=None):
        rows = rows or [mapping(self.host_port)]
        host_peer, guest_peer = _Peer(), _Peer()
        host = UdpForwarder(host_peer, True, rows, clock=clock, on_bytes=host_on_bytes)
        guest = UdpForwarder(guest_peer, False, rows, clock=clock, on_bytes=guest_on_bytes)
        host_peer.remote, guest_peer.remote = guest, host
        self.forwarders.extend([host, guest])
        records = await guest.start_guest()
        return host, guest, host_peer, guest_peer, records[0]["local_port"]

    async def client(self):
        transport, protocol = await self.loop.create_datagram_endpoint(
            _Client, local_addr=("127.0.0.1", 0))
        self.resources.append(transport)
        return protocol

    async def receive(self, client, timeout=2):
        return await asyncio.wait_for(client.received.get(), timeout)

    async def settle(self):
        for _ in range(8):
            await asyncio.sleep(0.01)

    async def test_multiple_clients_keep_their_own_replies(self):
        host, guest, _, _, port = await self.pair()
        first, second = await self.client(), await self.client()
        first.transport.sendto(b"first", ("127.0.0.1", port))
        second.transport.sendto(b"second", ("127.0.0.1", port))
        self.assertEqual(await self.receive(first), b"first")
        self.assertEqual(await self.receive(second), b"second")
        self.assertEqual(host.diagnostics()["active_associations"], 2)
        self.assertEqual(guest.diagnostics()["active_associations"], 2)
        self.assertEqual(host.diagnostics()["received_bytes"], 11)

    async def test_large_binary_udp_is_reassembled_without_retransmission(self):
        host, _, _, guest_peer, port = await self.pair()
        client = await self.client()
        payload = os.urandom(65507)
        client.transport.sendto(payload, ("127.0.0.1", port))
        reply = await self.receive(client)
        self.assertEqual(hashlib.sha256(reply).digest(), hashlib.sha256(payload).digest())
        self.assertEqual(len(guest_peer.frames), 67)
        self.assertTrue(all(len(frame) <= 1000 for frame in guest_peer.frames))
        self.assertEqual(host.diagnostics()["received_packets"], 1)

    async def test_fragment_sender_yields_to_bounded_quic_queue(self):
        host, guest, host_peer, guest_peer, port = await self.pair()
        host_peer.pending_limit = guest_peer.pending_limit = 8
        client = await self.client()
        payload = os.urandom(9000)
        client.transport.sendto(payload, ("127.0.0.1", port))
        self.assertEqual(await self.receive(client), payload)
        await self.settle()
        self.assertEqual(guest.diagnostics()["sent_packets"], 1)
        self.assertEqual(host.diagnostics()["sent_packets"], 1)
        self.assertEqual(guest.diagnostics()["dropped_packets"], 0)
        self.assertEqual(host.diagnostics()["dropped_packets"], 0)
        self.assertLessEqual(guest_peer.peak_pending, 8)
        self.assertLessEqual(host_peer.peak_pending, 8)

    async def test_byte_callback_counts_payload_once_per_complete_packet(self):
        host_bytes, guest_bytes = [], []
        host, _, _, guest_peer, port = await self.pair(
            host_on_bytes=lambda sent, received: host_bytes.append((sent, received)),
            guest_on_bytes=lambda sent, received: guest_bytes.append((sent, received)),
        )
        client = await self.client()
        payload = os.urandom(9000)
        client.transport.sendto(payload, ("127.0.0.1", port))
        self.assertEqual(await self.receive(client), payload)
        await self.settle()
        for frame in guest_peer.frames:
            host.handle_datagram(frame)
        self.assertEqual(sorted(host_bytes), [(0, 9000), (9000, 0)])
        self.assertEqual(sorted(guest_bytes), [(0, 9000), (9000, 0)])

    async def test_empty_udp_payload_round_trip_uses_real_sockets(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server, \
                socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            server.setblocking(False)
            client.setblocking(False)
            server.bind(("127.0.0.1", 0))
            client.bind(("127.0.0.1", 0))
            host, guest, _, _, port = await self.pair(rows=[mapping(server.getsockname()[1])])

            async def echo_empty():
                data, addr = await self.loop.sock_recvfrom(server, 1)
                self.assertEqual(data, b"")
                server.sendto(data, addr)

            echo_task = asyncio.create_task(echo_empty())
            try:
                client.sendto(b"", ("127.0.0.1", port))
                reply, _ = await asyncio.wait_for(self.loop.sock_recvfrom(client, 1), 1)
                self.assertEqual(reply, b"")
                await asyncio.wait_for(echo_task, 1)
                await self.settle()
                self.assertEqual(host.diagnostics()["received_packets"], 1)
                self.assertEqual(guest.diagnostics()["received_packets"], 1)
            finally:
                echo_task.cancel()
                await asyncio.gather(echo_task, return_exceptions=True)

    async def test_two_peers_can_use_same_association_id_without_collision(self):
        first = await self.pair()
        second = await self.pair()
        client = await self.client()
        with patch("pontelan.ports.secrets.randbits", return_value=1234):
            client.transport.sendto(b"one-peer", ("127.0.0.1", first[4]))
            self.assertEqual(await self.receive(client), b"one-peer")
            client.transport.sendto(b"other-peer", ("127.0.0.1", second[4]))
            self.assertEqual(await self.receive(client), b"other-peer")
        first_assoc = first[0]._associations[1234]
        second_assoc = second[0]._associations[1234]
        self.assertNotEqual(first_assoc.transport.get_extra_info("sockname"),
                            second_assoc.transport.get_extra_info("sockname"))

    async def test_missing_fragment_expires_and_is_never_delivered(self):
        clock = _Clock()
        host, _, _, guest_peer, port = await self.pair(clock=clock)
        lost = []

        def lose_middle(frame):
            if HEADER.unpack_from(frame)[-2] == 1:
                lost.append(frame)
                return True
            return False

        guest_peer.drop = lose_middle
        client = await self.client()
        client.transport.sendto(os.urandom(4000), ("127.0.0.1", port))
        await self.settle()
        self.assertEqual(host.diagnostics()["reassembly_packets"], 1)
        clock.now += FRAGMENT_TTL + 0.01
        host._expire()
        self.assertEqual(host.diagnostics()["fragment_timeouts"], 1)
        host.handle_datagram(lost[0])
        await self.settle()
        self.assertTrue(self.echo.received.empty())
        self.assertTrue(client.received.empty())
        self.assertEqual(host.diagnostics()["active_associations"], 0)
        self.assertEqual(host.diagnostics()["reassembly_bytes"], 0)
        self.assertEqual(len(guest_peer.frames), 5)

    async def test_bind_failure_rolls_back_previous_listener(self):
        reserved = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        peer = _Peer()
        guest = UdpForwarder(peer, False, [mapping(self.host_port),
                             mapping(self.host_port + 1, local_port=port)])
        self.forwarders.append(guest)
        bound = []
        create_endpoint = self.loop.create_datagram_endpoint

        async def record_bound(*args, **kwargs):
            transport, protocol = await create_endpoint(*args, **kwargs)
            bound.append(transport.get_extra_info("sockname")[1])
            return transport, protocol

        try:
            with patch.object(self.loop, "create_datagram_endpoint", side_effect=record_bound):
                with self.assertRaises(OSError):
                    await guest.start_guest()
            self.assertEqual(guest.diagnostics()["local_listeners"], 0)
            self.assertEqual(guest.diagnostics()["pending_tasks"], 0)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as released:
                released.bind(("127.0.0.1", bound[0]))
        finally:
            reserved.close()

    async def test_map_binding_and_readiness_prevent_destination_open(self):
        peer = _Peer()
        host = UdpForwarder(peer, True, [mapping(self.host_port, protocol="tcp")])
        self.forwarders.append(host)
        frame = HEADER.pack(MAGIC, WIRE_VERSION, 0, 7, 1, 4, 0, 1) + b"data"
        host.handle_datagram(frame)
        self.assertEqual(host.diagnostics()["invalid_frames"], 1)
        self.assertEqual(host.diagnostics()["active_associations"], 0)
        host.mappings = normalize_ports([mapping(self.host_port)])
        peer.ready.clear()
        host.handle_datagram(frame)
        await self.settle()
        self.assertTrue(self.echo.received.empty())
        self.assertEqual(host.diagnostics()["active_associations"], 0)

    async def test_malformed_and_fragment_flood_remain_bounded(self):
        host, _, _, _, _ = await self.pair()
        rng = random.Random(91)
        for _ in range(1000):
            host.handle_datagram(rng.randbytes(rng.randrange(0, 1100)))
        for packet in range(1000):
            host.handle_datagram(HEADER.pack(MAGIC, WIRE_VERSION, 0, 17, packet,
                                            65507, 0, 67) + b"x" * FRAGMENT_PAYLOAD)
        diagnostics = host.diagnostics()
        self.assertLessEqual(diagnostics["reassembly_bytes"], MAX_BUFFER_BYTES)
        self.assertLessEqual(diagnostics["reassembly_packets"], MAX_REASSEMBLIES)
        self.assertEqual(diagnostics["active_associations"], 0)
        self.assertGreater(diagnostics["invalid_frames"], 900)

    async def test_expiry_cleans_guest_sources_and_host_sockets(self):
        clock = _Clock()
        host, guest, _, _, port = await self.pair(clock=clock)
        client = await self.client()
        client.transport.sendto(b"hello", ("127.0.0.1", port))
        self.assertEqual(await self.receive(client), b"hello")
        transport = next(iter(host._associations.values())).transport
        clock.now += ASSOCIATION_TTL + 1
        host._expire()
        guest._expire()
        self.assertTrue(transport.is_closing())
        self.assertEqual(host.diagnostics()["active_associations"], 0)
        self.assertEqual(guest.diagnostics()["active_associations"], 0)
        self.assertEqual(guest._sources, {})

    async def test_congestion_drops_remaining_fragments(self):
        host, guest, _, guest_peer, port = await self.pair()
        guest_peer.congested = True
        client = await self.client()
        client.transport.sendto(os.urandom(65507), ("127.0.0.1", port))
        await self.settle()
        self.assertEqual(guest_peer.frames, [])
        self.assertEqual(guest.diagnostics()["dropped_packets"], 1)
        self.assertEqual(guest.diagnostics()["pending_bytes"], 0)
        self.assertEqual(host.diagnostics()["active_associations"], 0)

    async def test_close_before_scheduled_open_and_late_callback(self):
        host, guest, _, _, _ = await self.pair()
        frame = HEADER.pack(MAGIC, WIRE_VERSION, 0, 7, 1, 4, 0, 1) + b"data"
        host.handle_datagram(frame)
        await host.close()
        await host.close()
        host.handle_datagram(frame)
        guest._local_packet(0, b"late", ("127.0.0.1", 1234))
        await guest.close()
        self.assertTrue(self.echo.received.empty())
        self.assertEqual(host.diagnostics()["active_associations"], 0)
        self.assertEqual(host.diagnostics()["pending_tasks"], 0)
        self.assertEqual(host.diagnostics()["pending_bytes"], 0)

    async def test_close_during_open_cleans_new_socket(self):
        host, _, _, _, _ = await self.pair()
        opening = asyncio.Event()
        release = asyncio.Event()
        opened = []
        create_endpoint = self.loop.create_datagram_endpoint

        async def delayed_endpoint(*args, **kwargs):
            transport, protocol = await create_endpoint(*args, **kwargs)
            opened.append(transport)
            opening.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # Simulate a completed endpoint whose result races cancellation.
                pass
            return transport, protocol

        frame = HEADER.pack(MAGIC, WIRE_VERSION, 0, 7, 1, 4, 0, 1) + b"data"
        with patch.object(self.loop, "create_datagram_endpoint", side_effect=delayed_endpoint):
            host.handle_datagram(frame)
            await asyncio.wait_for(opening.wait(), 1)
            await host.close()
        self.assertTrue(opened[0].is_closing())
        self.assertTrue(self.echo.received.empty())
        self.assertEqual(host.diagnostics()["pending_bytes"], 0)

    async def test_connected_host_socket_rejects_unrelated_udp_sender(self):
        host, guest, _, _, port = await self.pair()
        client = await self.client()
        attacker = await self.client()
        client.transport.sendto(b"valid", ("127.0.0.1", port))
        self.assertEqual(await self.receive(client), b"valid")
        host_address = next(iter(host._associations.values())).transport.get_extra_info("sockname")
        attacker.transport.sendto(b"injected", host_address)
        await self.settle()
        self.assertTrue(client.received.empty())
        self.assertEqual(guest.diagnostics()["received_packets"], 1)

    async def test_host_associations_are_bounded_and_mapping_cannot_change(self):
        host, _, _, _, _ = await self.pair(rows=[mapping(self.host_port),
                                                    mapping(self.host_port + 1)])
        for ident in range(1, MAX_ASSOCIATIONS + 2):
            frame = HEADER.pack(MAGIC, WIRE_VERSION, 0, ident, 1, 1, 0, 1) + b"x"
            host.handle_datagram(frame)
            await asyncio.sleep(0)
        await self.settle()
        self.assertEqual(host.diagnostics()["active_associations"], MAX_ASSOCIATIONS)
        initial_invalid = host.diagnostics()["invalid_frames"]
        frame = HEADER.pack(MAGIC, WIRE_VERSION, 1, 1, 2, 1, 0, 1) + b"y"
        host.handle_datagram(frame)
        self.assertEqual(host.diagnostics()["invalid_frames"], initial_invalid + 1)

    async def test_duplicates_and_inconsistent_packet_metadata_are_dropped(self):
        host, _, _, _, _ = await self.pair()
        first = HEADER.pack(MAGIC, WIRE_VERSION, 0, 12, 1, 2000, 0, 3) + b"a" * FRAGMENT_PAYLOAD
        host.handle_datagram(first)
        host.handle_datagram(first)
        inconsistent = HEADER.pack(MAGIC, WIRE_VERSION, 0, 12, 1, 2001, 1, 3) + b"b" * FRAGMENT_PAYLOAD
        host.handle_datagram(inconsistent)
        self.assertEqual(host.diagnostics()["invalid_frames"], 1)
        self.assertEqual(host.diagnostics()["reassembly_bytes"], 0)
        self.assertEqual(host.diagnostics()["reassembly_packets"], 0)


if __name__ == "__main__":
    unittest.main()
