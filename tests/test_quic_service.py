"""Full room service integration: HTTP signaling, ICE, mutual TLS, QUIC, TCP/UDP."""
import asyncio
import contextlib
import hashlib
import os
import unittest

from pontelan.core import TunnelService
from pontelan.identity import Identity, TrustStore
from pontelan.event_loop import NetworkEventLoop
from test_tunnel import LocalNtfy, wait_event


class EchoUdp(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.transport.sendto(data, addr)


class UdpClient(asyncio.DatagramProtocol):
    def __init__(self):
        self.received = asyncio.Queue()

    def datagram_received(self, data, addr):
        self.received.put_nowait(data)


class QuicServiceTests(unittest.IsolatedAsyncioTestCase):
    def _setupAsyncioRunner(self):
        # Exercise the application's precise Windows pacing clock, with debug
        # checks retained. The default loop uses a coarse 15.6 ms clock here.
        self._asyncioRunner = asyncio.Runner(debug=True, loop_factory=NetworkEventLoop)

    async def asyncSetUp(self):
        self.signal = LocalNtfy()
        await self.signal.start()
        self.services, self.events, self.clients = [], {}, []
        self.server_writers = set()
        self.server_tasks = set()
        self.game = await asyncio.start_server(self.echo, "127.0.0.1", 0)
        self.other = await asyncio.start_server(self.echo, "127.0.0.1", 0)
        self.udp, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            EchoUdp, local_addr=("127.0.0.1", 0))
        self.host = self.service()

    async def echo(self, reader, writer):
        self.server_writers.add(writer)
        self.server_tasks.add(asyncio.current_task())
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
            writer.write(b"done")
            await writer.drain()
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            self.server_writers.discard(writer)
            self.server_tasks.discard(asyncio.current_task())

    def service(self, transport="quic", identity=None):
        events = []
        service = TunnelService(events.append, endpoint=self.signal.url, stun=False,
                                identity=identity or Identity.generate(), trust_store=TrustStore.in_memory(),
                                transport=transport)
        self.services.append(service)
        self.events[service] = events
        return service

    async def host_room(self, ports=None):
        await self.host.host(self.game.sockets[0].getsockname()[1], "QUIC test", ports)

    async def join(self, guest=None, remember=False, approve=True, code=None):
        guest = guest or self.service()
        await guest.join(code or self.host.code, name="Test peer")
        peer_id = guest.bus.peer_id
        if approve:
            await wait_event(self.events[self.host], lambda e: e.get("type") == "admission"
                and e.get("state") == "pending" and e.get("peer_id") == peer_id)
            await self.host.approve(peer_id, remember)
            event = await wait_event(self.events[guest], lambda e: e.get("type") == "listening")
            self.assertEqual(event["transport"], guest.transport)
            return guest, event
        return guest, None

    async def stream(self, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self.clients.append(writer)
        return reader, writer

    async def echo_bytes(self, stream, data):
        reader, writer = stream
        writer.write(data)
        await writer.drain()
        received = await asyncio.wait_for(reader.readexactly(len(data)), 30)
        self.assertEqual(hashlib.sha256(received).digest(), hashlib.sha256(data).digest())

    async def udp_client(self):
        protocol = UdpClient()
        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: protocol, local_addr=("127.0.0.1", 0))
        self.clients.append(transport)
        return transport, protocol

    async def asyncTearDown(self):
        for client in self.clients:
            client.close()
        for service in reversed(self.services):
            await asyncio.wait_for(service.stop(), 10)
        for writer in list(self.server_writers):
            writer.transport.abort()
        for server in (self.game, self.other):
            server.close()
            await server.wait_closed()
        for task in list(self.server_tasks):
            task.cancel()
        await asyncio.gather(*self.server_tasks, return_exceptions=True)
        self.udp.close()
        await self.signal.close()

    async def test_approval_then_large_bytes_and_tcp_half_close_over_quic(self):
        await self.host_room()
        guest, _ = await self.join(approve=False)
        await wait_event(self.events[self.host], lambda e: e.get("type") == "admission")
        self.assertFalse(self.host.peers)
        self.assertIsNone(guest.server)
        await self.host.approve(guest.bus.peer_id)
        await wait_event(self.events[guest], lambda e: e.get("type") == "listening")
        stream = await self.stream(guest.local_port)
        await self.echo_bytes(stream, os.urandom(1_000_000))
        stream[1].write_eof()
        self.assertEqual(await asyncio.wait_for(stream[0].read(), 5), b"done")
        peer = self.host.peers[guest.bus.peer_id]
        self.assertEqual(peer.transport, "quic")
        self.assertIn("QUIC", peer.route())

    async def test_extra_tcp_and_udp_are_real_and_published_ports_match_listeners(self):
        await self.host_room([
            {"name": "Extra TCP", "protocol": "tcp", "host_port": self.other.sockets[0].getsockname()[1], "local_port": 0},
            {"name": "Voice UDP", "protocol": "udp", "host_port": self.udp.get_extra_info("sockname")[1], "local_port": 0},
        ])
        guest, event = await self.join()
        self.assertEqual(len(event["ports"]), 2)
        extra_tcp = next(p for p in event["ports"] if p["protocol"] == "tcp")
        extra_udp = next(p for p in event["ports"] if p["protocol"] == "udp")
        await self.echo_bytes(await self.stream(extra_tcp["local_port"]), os.urandom(75000))
        transport, protocol = await self.udp_client()
        payload = os.urandom(512)
        transport.sendto(payload, ("127.0.0.1", extra_udp["local_port"]))
        self.assertEqual(await asyncio.wait_for(protocol.received.get(), 5), payload)
        payload = os.urandom(9000)
        transport.sendto(payload, ("127.0.0.1", extra_udp["local_port"]))
        self.assertEqual(await asyncio.wait_for(protocol.received.get(), 5), payload)
        self.assertTrue(all(p["local_address"] == f"127.0.0.1:{p['local_port']}" for p in event["ports"]))

    async def test_invite_rotation_keeps_same_quic_tcp_and_udp_alive(self):
        await self.host_room([{"name": "UDP", "protocol": "udp",
            "host_port": self.udp.get_extra_info("sockname")[1], "local_port": 0}])
        guest, event = await self.join()
        tcp = await self.stream(guest.local_port)
        await self.echo_bytes(tcp, b"before")
        old = self.host.code
        await self.host.renew_invite()
        self.assertNotEqual(old, self.host.code)
        await self.echo_bytes(tcp, b"after")
        transport, protocol = await self.udp_client()
        transport.sendto(b"still here", ("127.0.0.1", event["ports"][0]["local_port"]))
        self.assertEqual(await asyncio.wait_for(protocol.received.get(), 5), b"still here")

    async def test_removal_closes_only_target_and_revokes_trusted_quic_identity(self):
        await self.host_room()
        first, _ = await self.join(remember=True)
        second, _ = await self.join()
        stream = await self.stream(second.local_port)
        identity = first.identity
        await self.host.remove_peer(first.bus.peer_id)
        await wait_event(self.events[first], lambda e: e.get("state") == "stopped")
        self.assertFalse(self.host.trust_store.is_trusted(identity.public_key))
        await self.echo_bytes(stream, b"other peer stays")
        returning = self.service(identity=identity)
        await self.join(returning, approve=False)
        await wait_event(self.events[self.host], lambda e: e.get("type") == "admission"
            and e.get("peer_id") == returning.bus.peer_id and e.get("state") == "pending")
        self.assertIsNone(returning.server)

    async def test_legacy_webrtc_guest_still_connects_but_does_not_get_extra_ports(self):
        await self.host_room([{"name": "UDP", "protocol": "udp", "host_port": 24454, "local_port": 0}])
        guest, event = await self.join(self.service("webrtc"))
        self.assertEqual(event["ports"], [])
        await self.echo_bytes(await self.stream(guest.local_port), b"compatible")

    async def test_occupied_extra_port_rolls_back_guest_listener_and_peer(self):
        occupied = await asyncio.start_server(self.echo, "127.0.0.1", 0)
        try:
            await self.host_room([{"name": "Busy TCP", "protocol": "tcp",
                "host_port": self.other.sockets[0].getsockname()[1],
                "local_port": occupied.sockets[0].getsockname()[1]}])
            guest, _ = await self.join(approve=False)
            await wait_event(self.events[self.host], lambda e: e.get("type") == "admission")
            await self.host.approve(guest.bus.peer_id)
            await wait_event(self.events[guest], lambda e: e.get("type") == "status" and e.get("state") == "stopped")
            self.assertIsNone(guest.server)
            self.assertFalse(any(e.get("type") == "listening" for e in self.events[guest]))
        finally:
            occupied.close()
            await occupied.wait_closed()


if __name__ == "__main__":
    unittest.main()
