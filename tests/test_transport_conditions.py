"""Real local TCP/ICE/DTLS/SCTP compatibility and stalled-reader checks.

The legacy side uses the frozen pre-admission sender, not a legacy executable.
Signaling retains the authenticated codec but delivers its envelopes in memory.
"""
import asyncio
import contextlib
import hashlib
import os
import socket
import unittest
from unittest.mock import patch

from pontelan import core
from pontelan.identity import Identity, TrustStore
from pontelan.event_loop import NetworkEventLoop
from qa_transport import MemorySignalBus, original_read, wait_for_listener, approve_fixture_request


class TransportConditionsTests(unittest.IsolatedAsyncioTestCase):
    def _setupAsyncioRunner(self):
        # This test runner owns the loop; no process-wide event-loop policy or
        # OS timer setting is changed, and no running application is touched.
        self._asyncioRunner = asyncio.Runner(debug=True, loop_factory=NetworkEventLoop)

    async def asyncSetUp(self):
        self.patch_bus = patch.object(core, 'SignalBus', MemorySignalBus)
        self.patch_bus.start()
        self.addCleanup(self.patch_bus.stop)
        self.services = []
        self.fixture_tasks = set()
        self.fixture_writers = set()
        self.client_writers = []
        self.stop_tasks = []
        self.server = None
        self.resume = asyncio.Event()
        self.stalled = asyncio.Event()
        self.stalled_bridge = None
        self.fixture_errors = []
        self.request_hash = None
        self.upload = os.urandom(400_017)
        self.download = os.urandom(500_029)

    async def asyncTearDown(self):
        self.resume.set()
        for writer in self.client_writers:
            writer.transport.abort()
        for writer in list(self.fixture_writers):
            writer.transport.abort()
        # A failed paused-reader test can leave accepted sockets blocked on
        # admission. Close their peers before waiting for the local listener.
        for service in reversed(self.services):
            for peer in list(service.peers.values()):
                for bridge in list(peer.streams):
                    if bridge.writer:
                        bridge.writer.transport.abort()
                await asyncio.wait_for(peer.close(), 5)
        for task in self.stop_tasks:
            try:
                await asyncio.wait_for(asyncio.shield(task), 5)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        for service in reversed(self.services):
            await asyncio.wait_for(service.stop(), 5)
        if self.server:
            self.server.close()
            await asyncio.wait_for(self.server.wait_closed(), 5)
        for task in list(self.fixture_tasks):
            task.cancel()
        await asyncio.gather(*list(self.fixture_tasks), return_exceptions=True)

    async def fixture(self, reader, writer):
        task = asyncio.current_task()
        self.fixture_tasks.add(task)
        self.fixture_writers.add(writer)
        try:
            operation = await reader.readexactly(1)
            if operation == b'C':
                data = await reader.read()
                self.request_hash = hashlib.sha256(data).digest()
                writer.write(self.download)
                await writer.drain()
            elif operation == b'S':
                # Constrain real loopback socket buffering so one MiB is
                # sufficient to reach writer.drain(), rather than assuming
                # that a paused application immediately blocks TCP writes.
                writer.get_extra_info('socket').setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                address = writer.get_extra_info('peername')
                for peer in self.host.peers.values():
                    for bridge in peer.streams:
                        if bridge.writer and bridge.writer.get_extra_info('sockname') == address:
                            self.stalled_bridge = bridge
                if self.stalled_bridge is None:
                    raise AssertionError('Could not identify the real stalled TCP bridge')
                outbound = self.stalled_bridge.writer
                outbound.get_extra_info('socket').setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
                outbound.transport.set_write_buffer_limits(high=8192, low=4096)
                writer.transport.pause_reading()
                self.stalled.set()
                await self.resume.wait()
                writer.get_extra_info('socket').setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
                outbound.get_extra_info('socket').setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 16)
                outbound.transport.set_write_buffer_limits(high=65536, low=16384)
                writer.transport.resume_reading()
                data = await reader.read()
                writer.write(hashlib.sha256(data).digest())
                await writer.drain()
            elif operation == b'E':
                data = await reader.read()
                writer.write(data)
                await writer.drain()
            else:
                raise AssertionError('Unexpected fixture operation')
        except (asyncio.CancelledError, asyncio.IncompleteReadError, OSError):
            pass
        except Exception as exc:
            self.fixture_errors.append(repr(exc))
        finally:
            writer.transport.resume_reading()
            writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 1)
            self.fixture_writers.discard(writer)
            self.fixture_tasks.discard(task)

    async def connect(self):
        self.server = await asyncio.start_server(self.fixture, '127.0.0.1', 0)
        self.host = core.TunnelService(lambda event: None, stun=False, identity=Identity.generate(), trust_store=TrustStore.in_memory())
        events = []
        self.guest = core.TunnelService(events.append, stun=False, identity=Identity.generate(), trust_store=TrustStore.in_memory())
        self.services.extend((self.host, self.guest))
        await self.host.host(self.server.sockets[0].getsockname()[1], 'Isolated transport conditions')
        await self.guest.join(self.host.code)
        await approve_fixture_request(self.host)
        await wait_for_listener(self.guest, events)
        peer = self.guest.peers['host']
        self.assertEqual(peer.pc.sctp.transport.state, 'connected')
        self.assertEqual(peer.pc.sctp.state, 'connected')
        self.assertEqual(peer.route(), 'host ↔ host')

    async def exchange(self, operation, data, timeout=20):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.guest.local_port)
        self.client_writers.append(writer)
        writer.write(operation + data)
        await writer.drain()
        writer.write_eof()
        answer = await asyncio.wait_for(reader.read(), timeout)
        writer.close()
        await writer.wait_closed()
        return answer

    async def compatibility(self, legacy_role):
        candidate_read = core.StreamBridge._read
        called = {'legacy': 0, 'candidate': 0}

        async def mixed_read(bridge):
            legacy = bridge.peer.owner.role == legacy_role
            called['legacy' if legacy else 'candidate'] += 1
            await (original_read(bridge) if legacy else candidate_read(bridge))

        with patch.object(core.StreamBridge, '_read', mixed_read):
            await self.connect()
            answer = await self.exchange(b'C', self.upload)
            self.assertEqual(len(answer), len(self.download))
            self.assertEqual(hashlib.sha256(answer).digest(), hashlib.sha256(self.download).digest())
            self.assertEqual(self.request_hash, hashlib.sha256(self.upload).digest())
            self.assertGreater(called['legacy'], 0)
            self.assertGreater(called['candidate'], 0)
            self.assertEqual(self.fixture_errors, [])

    async def test_legacy_host_sender_with_current_guest_sender_preserves_half_close_and_hashes(self):
        await self.compatibility('host')

    async def test_current_host_sender_with_legacy_guest_sender_preserves_half_close_and_hashes(self):
        await self.compatibility('guest')

    async def test_stop_closes_active_tcp_without_waiting_for_client_eof(self):
        await self.connect()
        _, writer = await asyncio.open_connection('127.0.0.1', self.guest.local_port)
        self.client_writers.append(writer)
        request = b'Econnection remains open'
        writer.write(request)
        await writer.drain()
        async with asyncio.timeout(5):
            while self.host.received < len(request):
                await asyncio.sleep(.01)
        self.assertFalse(writer.is_closing())
        peer = self.guest.peers['host']
        self.assertTrue(peer.streams)
        listener = self.guest.server
        stopping = asyncio.create_task(self.guest.stop())
        self.stop_tasks.append(stopping)
        try:
            await asyncio.wait_for(asyncio.shield(stopping), 1)
        except TimeoutError:
            self.fail('stop() waited for the accepted TCP client to close before closing its peer')
        self.assertIsNone(self.guest.server)
        self.assertFalse(listener.is_serving())
        self.assertFalse(self.guest.peers)
        self.assertIsNone(self.guest.bus)

    async def test_stop_aborts_paused_destination_without_waiting_for_reader_resume(self):
        await self.connect()
        _, writer = await asyncio.open_connection('127.0.0.1', self.guest.local_port)
        self.client_writers.append(writer)
        writer.write(b'S' + os.urandom(1 << 20))
        await asyncio.wait_for(self.stalled.wait(), 10)
        async with asyncio.timeout(10):
            while not (self.stalled_bridge.writer.transport.get_write_buffer_size() > 8192
                       and self.stalled_bridge.queued_bytes > 0):
                if self.fixture_errors:
                    self.fail(str(self.fixture_errors))
                await asyncio.sleep(.01)
        self.assertFalse(self.resume.is_set())
        self.assertFalse(writer.is_closing())
        bridge = self.stalled_bridge
        stopping = asyncio.create_task(self.host.stop())
        self.stop_tasks.append(stopping)
        await asyncio.wait_for(asyncio.shield(stopping), 1)
        try:
            await asyncio.wait_for(asyncio.shield(bridge.task), 1)
        except TimeoutError:
            self.fail('stopped tunnel left bridge.run waiting for a paused TCP writer to drain')
        self.assertFalse(self.resume.is_set())
        self.assertTrue(bridge.closed)
        self.assertTrue(bridge.writer.transport.is_closing())
        self.assertFalse(self.host.peers)

    async def test_real_stalled_destination_allows_unrelated_echo_then_resumes_exact_bytes(self):
        await self.connect()
        bulk = os.urandom(1 << 20)
        transfer = asyncio.create_task(self.exchange(b'S', bulk, timeout=30))
        try:
            await asyncio.wait_for(self.stalled.wait(), 10)
            peer = self.guest.peers['host']
            async with asyncio.timeout(10):
                while not (self.stalled_bridge.writer.transport.get_write_buffer_size() > 8192
                           and self.stalled_bridge.queued_bytes > 0
                           and peer.delivery.waiters):
                    if self.fixture_errors:
                        self.fail(str(self.fixture_errors))
                    await asyncio.sleep(.01)
            self.assertFalse(transfer.done())
            # Actual TCP request, reply and FINs traverse the same loaded SCTP
            # association while the bulk destination remains paused.
            message = os.urandom(64)
            try:
                echo = await asyncio.wait_for(self.exchange(b'E', message), 5)
            except TimeoutError:
                self.fail('Paused-reader echo blocked; delivery=' + str(peer.delivery.diagnostics())
                          + '; held=' + str(list(peer.delivery.held.values()))
                          + '; streams=' + str([(stream.credit, list(stream.pending_sizes), stream.queued_bytes,
                                               stream.closed) for stream in peer.streams]))
            self.assertEqual(echo, message)
            self.assertFalse(self.resume.is_set())
            self.assertFalse(transfer.done())
            self.resume.set()
            digest = await asyncio.wait_for(transfer, 30)
            self.assertEqual(digest, hashlib.sha256(bulk).digest())
            self.assertEqual(self.fixture_errors, [])
        finally:
            self.resume.set()
            if not transfer.done():
                transfer.cancel()
            await asyncio.gather(transfer, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
