"""Cancellation must release real partial listeners and the full QUIC peer."""
import asyncio
import contextlib
import unittest
from unittest.mock import patch

from pontelan.core import TunnelPeer, TunnelService
from pontelan.identity import TrustStore
from pontelan.ports import UdpForwarder


class QuicCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.services = []
        self.servers = []
        self.peers = []
        self.releases = []

    def guest(self):
        service = TunnelService(lambda event: None, stun=False,
                                trust_store=TrustStore.in_memory(), transport="quic")
        service.role = "guest"
        peer = TunnelPeer(service, "host", "Host")
        peer.ready.set()
        service.peers["host"] = peer
        self.services.append(service)
        self.peers.append(peer)
        return service, peer

    async def asyncTearDown(self):
        for release in self.releases:
            release.set()
        for service in self.services:
            await asyncio.wait_for(service.stop(), 3)
        # Own repro resources independently so a regression cannot leak them
        # into subsequent tests after the assertions expose the failure.
        for peer in self.peers:
            await asyncio.wait_for(peer.pc.close(), 3)
        for server in self.servers:
            server.close()
            await server.wait_closed()

    async def test_cancel_after_extra_tcp_bind_releases_every_listener(self):
        service, peer = self.guest()
        peer.port_mappings = [{"id": "p1", "name": "Extra TCP", "protocol": "tcp",
                               "host_port": 24454, "local_port": 0}]
        original = asyncio.base_events.Server._start_serving
        bound = []

        def cancel_second_listener(server):
            original(server)
            bound.append(server)
            self.servers.append(server)
            if len(bound) == 2:
                # Real asyncio listener creation/activation yields immediately
                # after binding; cancel at that exact suspension boundary.
                asyncio.current_task().cancel()

        with patch.object(asyncio.base_events.Server, "_start_serving", cancel_second_listener):
            listening = asyncio.create_task(service._listen(peer))
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(listening, 3)

        self.assertEqual(len(bound), 2)
        ports = [server.sockets[0].getsockname()[1] for server in bound]
        await asyncio.wait_for(service.stop(), 3)
        self.assertTrue(all(not server.is_serving() for server in bound))
        self.assertFalse(peer.extra_servers)
        self.assertIsNone(service.server)
        for port in ports:
            rebound = await asyncio.start_server(lambda reader, writer: writer.close(),
                                                 "127.0.0.1", port)
            self.servers.append(rebound)
            rebound.close()
            await rebound.wait_closed()

    async def test_cancel_close_during_udp_cleanup_still_releases_ice(self):
        service, peer = self.guest()
        with patch("aioice.ice.get_host_addresses", return_value=["127.0.0.1"]):
            await peer.pc.offer()
        self.assertTrue(peer.pc._ice._protocols)
        peer.udp = UdpForwarder(peer, False, [])
        entered, release = asyncio.Event(), asyncio.Event()
        self.releases.append(release)
        original = peer.udp.close

        async def gated_udp_close():
            await original()
            entered.set()
            await release.wait()

        peer.udp.close = gated_udp_close
        closing = asyncio.create_task(peer.close())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            closing.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await closing
        finally:
            release.set()
            with contextlib.suppress(asyncio.CancelledError):
                await closing

        await asyncio.wait_for(service.stop(), 3)
        self.assertEqual(peer.pc.connectionState, "closed")
        self.assertFalse(peer.pc._ice._protocols)
        self.assertTrue(peer._close_task.done())


if __name__ == "__main__":
    unittest.main()
