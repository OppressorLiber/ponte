"""Regression checks for cancellation races and bounded TCP flow control.

All sockets, signaling services, and WebRTC transports are replaced locally;
these tests do not contact ntfy, STUN, or even a loopback TCP listener.
"""
import asyncio
import contextlib
import struct
import unittest
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import patch

from pontelan import core


class FakeWriter:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class FakeBus:
    def __init__(self, *, gated=False):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False
        self.sent = []
        if not gated:
            self.release.set()

    async def start(self):
        self.entered.set()
        await self.release.wait()

    async def close(self):
        self.closed = True

    async def send(self, recipient, message):
        self.sent.append((recipient, message))


class FakeChannel:
    def __init__(self):
        self.readyState = "open"
        self.bufferedAmount = 0
        self.handlers = defaultdict(list)
        self.sent = []
        self.changed = asyncio.Event()

    def on(self, name, handler):
        self.handlers[name].append(handler)

    def send(self, data):
        self.sent.append(data)
        self.changed.set()

    def close(self):
        self.readyState = "closed"
        for handler in self.handlers["close"]:
            handler()

    async def wait_for_payloads(self, count):
        async def wait():
            while len([frame for frame in self.sent if frame[0] == 0]) < count:
                self.changed.clear()
                await self.changed.wait()
        await asyncio.wait_for(wait(), 1)


class FakeReader:
    def __init__(self, chunks):
        self.chunks = asyncio.Queue()
        for chunk in chunks:
            self.chunks.put_nowait(chunk)

    async def read(self, count):
        # An empty queue deliberately blocks rather than generating EOF.
        chunk = await self.chunks.get()
        assert len(chunk) <= count
        return chunk


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_during_host_tcp_validation_does_not_open_room(self):
        events = []
        service = core.TunnelService(events.append, stun=False)
        entered, release = asyncio.Event(), asyncio.Event()
        writer = FakeWriter()

        async def connection(*args, **kwargs):
            entered.set()
            await release.wait()
            return object(), writer

        with patch.object(core.asyncio, "open_connection", connection), \
                patch.object(core, "SignalBus") as bus_factory:
            opening = asyncio.create_task(service.host(25565))
            await asyncio.wait_for(entered.wait(), 1)
            await service.stop()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(opening, 1)
            bus_factory.assert_not_called()

        self.assertTrue(writer.closed)
        self.assertTrue(service.stopping)
        self.assertFalse(service.starting)
        self.assertIsNone(service.bus)
        self.assertIsNone(service.metrics_task)
        self.assertFalse(any(event["type"] == "room" for event in events))
        self.assertFalse(any(event.get("state") == "hosting" for event in events))

    async def _stop_during_signal_start(self, role):
        events = []
        service = core.TunnelService(events.append, stun=False)
        buses = []
        writers = []
        bus_created = asyncio.Event()

        async def connection(*args, **kwargs):
            writer = FakeWriter()
            writers.append(writer)
            return object(), writer

        def bus_factory(*args, **kwargs):
            bus = FakeBus(gated=not buses)
            buses.append(bus)
            bus_created.set()
            return bus

        with patch.object(core.asyncio, "open_connection", connection), \
                patch.object(core, "SignalBus", side_effect=bus_factory), \
                patch.object(core, "TunnelPeer") as peer_factory:
            operation = (service.host(25565) if role == "host"
                         else service.join(core.new_code()))
            opening = asyncio.create_task(operation)
            try:
                await asyncio.wait_for(bus_created.wait(), 1)
                await asyncio.wait_for(buses[0].entered.wait(), 1)
                await service.stop()
                self.assertTrue(buses[0].closed)
                self.assertIsNone(service.bus)
                self.assertFalse(service.peers)
                self.assertIsNone(service.metrics_task)

                # Begin a replacement before the old await completes. Its cleanup
                # must close only the stale bus, never the newly active room.
                code = await service.host(25566, "Replacement")
                replacement = service.bus
                buses[0].release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(opening, 1)
                self.assertIs(service.bus, replacement)
                self.assertFalse(replacement.closed)
                self.assertEqual(service.code, code)
                self.assertEqual(service.role, "host")
                self.assertFalse(service.stopping)
                self.assertFalse(service.starting)
                self.assertEqual(len([e for e in events if e["type"] == "room"]), 1)
                peer_factory.assert_not_called()
            finally:
                for bus in buses:
                    bus.release.set()
                if not opening.done():
                    opening.cancel()
                await asyncio.gather(opening, return_exceptions=True)
                await service.stop()
        self.assertTrue(all(writer.closed for writer in writers))

    async def test_stop_during_host_signal_start_preserves_replacement(self):
        await self._stop_during_signal_start("host")

    async def test_stop_during_guest_signal_start_preserves_replacement(self):
        await self._stop_during_signal_start("guest")

    async def test_old_peer_close_does_not_remove_replacement(self):
        service = core.TunnelService(lambda event: None, stun=False)
        entered, release = asyncio.Event(), asyncio.Event()

        async def close_pc():
            entered.set()
            await release.wait()

        pc = SimpleNamespace(on=lambda *args: None, close=close_pc)
        with patch.object(core, "RTCPeerConnection", return_value=pc):
            old_peer = core.TunnelPeer(service, "host", "Old host")
        service.peers["host"] = old_peer
        closing = asyncio.create_task(old_peer.close())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await service.stop()
            replacement = object()
            service.peers["host"] = replacement
            release.set()
            await asyncio.wait_for(closing, 1)
            self.assertIs(service.peers.get("host"), replacement)
        finally:
            release.set()
            await asyncio.gather(closing, return_exceptions=True)
            service.peers.clear()


class FlowControlTests(unittest.IsolatedAsyncioTestCase):
    def bridge(self, chunks=()):
        owner = SimpleNamespace(sent=0, received=0)
        peer = SimpleNamespace(owner=owner, streams=set())
        channel = FakeChannel()
        bridge = core.StreamBridge(peer, channel, FakeReader(chunks))
        peer.streams.add(bridge)
        return bridge, channel

    async def test_128_small_frames_and_fin_are_received_without_overflow(self):
        bridge, _ = self.bridge()
        for _ in range(128):
            bridge._message(b"\x00x")
        bridge._message(b"\x01")
        self.assertFalse(bridge.closed)
        self.assertEqual(bridge.queued_bytes, 128)
        self.assertEqual(bridge.queued_frames, 128)
        self.assertEqual(bridge.queue.qsize(), 129)
        for _ in range(128):
            self.assertEqual(bridge.queue.get_nowait(), b"\x00x")
        self.assertEqual(bridge.queue.get_nowait(), b"\x01")
        bridge.close()

    async def test_receive_frame_limit_rejects_129th_pending_payload(self):
        bridge, channel = self.bridge()
        for _ in range(129):
            bridge._message(b"\x00x")
        self.assertTrue(bridge.closed)
        self.assertEqual(channel.readyState, "closed")
        self.assertEqual(bridge.queue.qsize(), 128)

    async def test_receive_byte_window_rejects_overflow_before_queueing(self):
        bridge, _ = self.bridge()
        payload = b"x" * core.CHUNK
        for _ in range(core.WINDOW // core.CHUNK):
            bridge._message(b"\x00" + payload)
        self.assertFalse(bridge.closed)
        self.assertEqual(bridge.queued_bytes, core.WINDOW)
        bridge._message(b"\x00x")
        self.assertTrue(bridge.closed)
        self.assertEqual(bridge.queue.qsize(), core.WINDOW // core.CHUNK)

    async def _send_until_credit_returned(self, chunks, initial_count, ack_bytes):
        bridge, channel = self.bridge(chunks)
        sending = asyncio.create_task(bridge._read())
        try:
            await channel.wait_for_payloads(initial_count)
            await asyncio.sleep(0)
            self.assertFalse(sending.done())
            self.assertEqual(len(channel.sent), initial_count)
            self.assertEqual(bridge.credit,
                             core.WINDOW - sum(len(chunk) for chunk in chunks[:initial_count]))
            bridge._message(b"\x03" + struct.pack("!I", ack_bytes))
            await channel.wait_for_payloads(initial_count + 1)
            self.assertFalse(bridge.closed)
            self.assertEqual(channel.sent[-1], b"\x00" + chunks[initial_count])
            self.assertLessEqual(len(bridge.pending_sizes), 128)
            self.assertGreaterEqual(bridge.credit, 0)
            self.assertEqual(bridge.credit + sum(bridge.pending_sizes), core.WINDOW)
        finally:
            sending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sending
            bridge.close()

    async def test_sender_waits_for_frame_credit_and_resumes_after_ack(self):
        await self._send_until_credit_returned([b"x"] * 129, 128, 1)

    async def test_sender_waits_for_byte_credit_and_resumes_after_ack(self):
        frames = core.WINDOW // core.SEND_CHUNK
        with patch.object(core, 'PEER_SEND_BUDGET', core.WINDOW):
            await self._send_until_credit_returned(
                [b"x" * core.SEND_CHUNK] * (frames + 1), frames, core.SEND_CHUNK)


if __name__ == "__main__":
    unittest.main()
