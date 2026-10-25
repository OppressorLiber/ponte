"""Bound local channel queues without charging TCP data for admission wait time."""
import asyncio
import struct
import unittest
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import patch

from pontelan import core
from pontelan.send_buffer import SendBuffer


class FakeClock:
    def __init__(self, now=10.0):
        self.now = now
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.now


class FakeChannel:
    def __init__(self, name="data", *, buffered=0, state="open", sent_order=None):
        self.name = name
        self.bufferedAmount = buffered
        self.bufferedAmountLowThreshold = 0
        self.readyState = state
        self.handlers = defaultdict(list)
        self.sent = []
        self.sent_event = asyncio.Event()
        self.sent_order = sent_order

    def on(self, event, callback):
        self.handlers[event].append(callback)

    def send(self, data):
        if self.readyState != "open":
            raise ConnectionError("closed test channel")
        self.sent.append(data)
        self.bufferedAmount += len(data)
        if self.sent_order is not None:
            self.sent_order.append(self.name)
        self.sent_event.set()

    def drain(self, count=None):
        before = self.bufferedAmount
        self.bufferedAmount = max(0, before - (before if count is None else count))
        if before > self.bufferedAmountLowThreshold >= self.bufferedAmount:
            for callback in self.handlers["bufferedamountlow"]:
                callback()

    def close(self):
        self.readyState = "closed"
        for callback in self.handlers["close"]:
            callback()


class FakeReader:
    def __init__(self, payload):
        self.chunks = asyncio.Queue()
        self.chunks.put_nowait(payload)
        self.read_event = asyncio.Event()

    async def read(self, count):
        payload = await self.chunks.get()
        assert len(payload) <= count
        self.read_event.set()
        return payload


class FakeWriter:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class SendBufferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tasks = []
        self.buffers = []

    async def asyncTearDown(self):
        for buffer in self.buffers:
            buffer.close()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def buffer(self, channels, limit=8, **kwargs):
        buffer = SendBuffer(lambda: sum(channel.bufferedAmount for channel in channels
                                       if channel.readyState == "open"), limit, **kwargs)
        self.buffers.append(buffer)
        for channel in channels:
            channel.on("bufferedamountlow", buffer.changed.set)
            channel.on("close", buffer.changed.set)
        return buffer

    def sending(self, buffer, channel, data=b"data"):
        task = asyncio.create_task(buffer.send(channel, data))
        self.tasks.append(task)
        return task

    async def test_concurrent_channels_share_one_cap_and_preserve_each_payload(self):
        channels = [FakeChannel(str(index)) for index in range(3)]
        buffer = self.buffer(channels)
        payloads = [b"\x00\xffAB", b"CD\x00\xfe", b"EFGH"]
        tasks = [self.sending(buffer, channel, data)
                 for channel, data in zip(channels, payloads)]
        await asyncio.sleep(0)
        self.assertEqual([task.done() for task in tasks], [True, True, False])
        self.assertEqual(sum(channel.bufferedAmount for channel in channels), buffer.limit)
        self.assertEqual([channel.sent for channel in channels],
                         [[payloads[0]], [payloads[1]], []])
        channels[0].drain()
        await asyncio.wait_for(tasks[2], 1)
        self.assertEqual(channels[2].sent, [payloads[2]])
        self.assertEqual(sum(channel.bufferedAmount for channel in channels), buffer.limit)

    async def test_cancelling_active_or_queued_sender_preserves_fifo_and_capacity(self):
        for cancelled_index in (0, 1):
            with self.subTest(cancelled_index=cancelled_index):
                sent_order = []
                blocker = FakeChannel(buffered=8)
                channels = [FakeChannel(str(index), sent_order=sent_order) for index in range(3)]
                buffer = self.buffer([blocker, *channels])
                tasks = [self.sending(buffer, channel) for channel in channels]
                await asyncio.sleep(0)
                self.assertFalse(any(task.done() for task in tasks))
                tasks[cancelled_index].cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await tasks[cancelled_index]
                blocker.drain()
                remaining = [task for index, task in enumerate(tasks) if index != cancelled_index]
                await asyncio.wait_for(asyncio.gather(*remaining), 1)
                self.assertEqual(sent_order, [str(index) for index in range(3)
                                              if index != cancelled_index])
                self.assertFalse(channels[cancelled_index].sent)
                self.assertEqual(sum(channel.bufferedAmount for channel in channels), buffer.limit)
                self.assertFalse(buffer.lock.locked())

    async def test_spurious_wakeup_rechecks_capacity_without_sending(self):
        blocker, channel = FakeChannel(buffered=8), FakeChannel()
        buffer = self.buffer([blocker, channel])
        task = self.sending(buffer, channel)
        await asyncio.sleep(0)
        buffer.changed.set()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertFalse(channel.sent)
        blocker.drain()
        await asyncio.wait_for(task, 1)
        self.assertEqual(channel.sent, [b"data"])

    async def test_capacity_notification_between_check_and_wait_is_not_lost(self):
        channel = FakeChannel()
        checks = []

        def buffered_bytes():
            checks.append(True)
            if len(checks) == 1:
                buffer.changed.set()
                return 8
            return 0

        buffer = SendBuffer(buffered_bytes, limit=8)
        self.buffers.append(buffer)
        await asyncio.wait_for(buffer.send(channel, b"data"), 1)
        self.assertEqual(len(checks), 2)
        self.assertEqual(channel.sent, [b"data"])

    async def test_close_wakes_all_senders_and_rejects_future_sends(self):
        blocker = FakeChannel(buffered=8)
        channels = [FakeChannel(str(index)) for index in range(3)]
        buffer = self.buffer([blocker, *channels])
        tasks = [self.sending(buffer, channel) for channel in channels]
        await asyncio.sleep(0)
        buffer.close()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 1)
        self.assertTrue(all(isinstance(result, ConnectionError) for result in results))
        self.assertFalse(any(channel.sent for channel in channels))
        with self.assertRaises(ConnectionError):
            await buffer.send(channels[0], b"next")
        self.assertFalse(buffer.lock.locked())

    async def test_closed_channel_fails_before_or_during_capacity_wait(self):
        for initially_closed in (True, False):
            with self.subTest(initially_closed=initially_closed):
                blocker = FakeChannel(buffered=8)
                channel = FakeChannel(state="closed" if initially_closed else "open")
                buffer = self.buffer([blocker, channel])
                task = self.sending(buffer, channel)
                await asyncio.sleep(0)
                if not initially_closed:
                    self.assertFalse(task.done())
                    channel.close()
                with self.assertRaises(ConnectionError):
                    await asyncio.wait_for(task, 1)
                self.assertFalse(channel.sent)
                blocker.drain()
                await asyncio.wait_for(buffer.send(blocker, b"next"), 1)
                self.assertEqual(blocker.sent, [b"next"])

    async def test_oversized_message_is_rejected_without_blocking_other_senders(self):
        channel = FakeChannel()
        buffer = self.buffer([channel])
        with self.assertRaises(ValueError):
            await buffer.send(channel, b"123456789")
        self.assertFalse(channel.sent)
        await buffer.send(channel, b"12345678")
        self.assertEqual(channel.sent, [b"12345678"])

    async def test_send_timestamp_is_taken_after_capacity_becomes_available(self):
        clock = FakeClock()
        blocker, channel = FakeChannel(buffered=8), FakeChannel()
        buffer = self.buffer([blocker, channel], clock=clock)
        task = self.sending(buffer, channel)
        await asyncio.sleep(0)
        self.assertEqual(clock.calls, 0)
        clock.now = 37.0
        blocker.drain()
        self.assertEqual(await asyncio.wait_for(task, 1), 37.0)
        self.assertEqual(clock.calls, 1)
        self.assertEqual(channel.sent, [b"data"])

    def peer(self):
        peer = SimpleNamespace(streams=set(), control=None, owner=SimpleNamespace(sent=0),
                               _control_message=lambda raw: None, _opened=lambda: None)
        peer.send_buffer = SendBuffer(lambda: core.outgoing_buffered(peer), limit=16)
        self.buffers.append(peer.send_buffer)
        return peer

    async def test_control_buffer_counts_toward_cap_and_its_low_event_wakes_data(self):
        peer = self.peer()
        control = FakeChannel(buffered=12, state="connecting")
        core.TunnelPeer._control(peer, control)
        control.readyState = "open"
        channel = FakeChannel(buffered=4)
        bridge = core.StreamBridge(peer, channel)
        peer.streams.add(bridge)
        closed = FakeChannel(buffered=100, state="closed")
        peer.streams.add(core.StreamBridge(peer, closed))
        self.assertEqual(core.outgoing_buffered(peer), 16)
        task = self.sending(peer.send_buffer, channel, b"\x00test")
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertEqual(control.bufferedAmountLowThreshold, 0)
        self.assertEqual(channel.bufferedAmountLowThreshold, 0)
        control.drain()
        await asyncio.wait_for(task, 1)
        self.assertEqual(channel.sent, [b"\x00test"])
        self.assertEqual(core.outgoing_buffered(peer), 9)

    async def test_bridge_credit_rtt_excludes_gate_wait_and_keeps_bytes_intact(self):
        clock = FakeClock()
        peer = self.peer()
        peer.send_buffer.clock = clock
        peer.control = FakeChannel(buffered=16)
        channel = FakeChannel()
        payload = b"\x00\xff\x01\x02Ponte\x00\xfe"
        bridge = core.StreamBridge(peer, channel, reader=FakeReader(payload))
        peer.streams.add(bridge)
        task = asyncio.create_task(bridge._read())
        self.tasks.append(task)
        await asyncio.sleep(0)
        self.assertFalse(channel.sent)
        self.assertFalse(bridge.pending_times)
        self.assertEqual(clock.calls, 0)
        clock.now = 37.0
        peer.control.bufferedAmount = 0
        peer.send_buffer.changed.set()
        await asyncio.wait_for(channel.sent_event.wait(), 1)
        self.assertEqual(channel.sent, [b"\x00" + payload])
        self.assertEqual(list(bridge.pending_times), [37.0])
        self.assertEqual(peer.owner.sent, len(payload))
        self.assertEqual(peer.delivery.held[bridge], len(payload))
        clock.now = 37.25
        with patch.object(core.time, "perf_counter", clock):
            bridge._message(b"\x03" + struct.pack("!I", len(payload)))
        self.assertEqual(peer.delivery.base_rtt, .25)
        self.assertEqual(bridge.credit, core.WINDOW)
        self.assertFalse(bridge.pending_times)
        self.assertFalse(bridge.pending_sizes)
        self.assertEqual(peer.delivery.used, 0)

    async def test_retired_open_channel_is_not_double_counted_and_repeated_retire_is_safe(self):
        peer = self.peer()
        old_channel = FakeChannel(buffered=8)
        old_bridge = core.StreamBridge(peer, old_channel)
        new_channel = FakeChannel()
        new_bridge = core.StreamBridge(peer, new_channel)
        peer.streams.update((old_bridge, new_bridge))
        peer.send_buffer.retire(old_channel)
        peer.send_buffer.retire(old_channel)
        self.assertEqual(peer.send_buffer.retired, {old_channel})
        self.assertEqual(peer.send_buffer.retired_bytes(), 0)
        await asyncio.wait_for(peer.send_buffer.send(new_channel, b"12345678"), 1)
        self.assertEqual(core.outgoing_buffered(peer), 16)
        old_channel.bufferedAmount = 0
        self.assertEqual(peer.send_buffer.retired_bytes(), 0)
        self.assertFalse(peer.send_buffer.retired)

    async def test_retire_prunes_silently_drained_channels_without_any_data_send(self):
        clock = FakeClock()
        buffer = self.buffer([], clock=clock)
        previous = None
        for _ in range(4):
            if previous is not None:
                previous.bufferedAmount = 0
            channel = FakeChannel(buffered=5, state="closed")
            buffer.retire(channel)
            # No send(), low event or explicit retired_bytes() call may be
            # needed to discard old references during receive-only churn.
            self.assertEqual(buffer.retired, {channel})
            self.assertFalse(channel.sent)
            previous = channel
        previous.bufferedAmount = 0
        buffer.retire(FakeChannel(state="closed"))
        self.assertFalse(buffer.retired)
        self.assertEqual(clock.calls, 0)

    async def test_aborted_stream_queue_stays_charged_after_removal_and_silent_drain_recovers(self):
        for state in ("closing", "closed"):
            with self.subTest(state=state):
                peer = self.peer()
                old_channel = FakeChannel(buffered=16)
                old_bridge = core.StreamBridge(peer, old_channel)
                new_channel = FakeChannel()
                new_bridge = core.StreamBridge(peer, new_channel)
                peer.streams.update((old_bridge, new_bridge))
                old_bridge.close()
                old_channel.readyState = state
                # Established aiortc channels can retain queued bytes after
                # close removes listeners and bridge.run removes the stream.
                old_channel.handlers.clear()
                peer.streams.discard(old_bridge)
                self.assertEqual(core.outgoing_buffered(peer), 0)
                self.assertEqual(peer.send_buffer.retired_bytes(), 16)
                task = self.sending(peer.send_buffer, new_channel)
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertFalse(new_channel.sent)
                old_channel.bufferedAmount = 0
                # Deliberately emit no notification: the exceptional retired
                # queue must be rechecked after the final listeners disappear.
                await asyncio.wait_for(task, .5)
                self.assertEqual(new_channel.sent, [b"data"])
                self.assertFalse(peer.send_buffer.retired)
                self.assertEqual(core.outgoing_buffered(peer), 4)

    async def test_cancelling_sender_waiting_on_retired_bytes_does_not_orphan_fifo(self):
        old_channel = FakeChannel(buffered=8, state="closed")
        first, second = FakeChannel("first"), FakeChannel("second")
        buffer = self.buffer([first, second])
        buffer.retire(old_channel)
        cancelled = self.sending(buffer, first)
        surviving = self.sending(buffer, second)
        await asyncio.sleep(0)
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        old_channel.bufferedAmount = 0
        await asyncio.wait_for(surviving, .5)
        self.assertFalse(first.sent)
        self.assertEqual(second.sent, [b"data"])
        self.assertFalse(buffer.retired)
        self.assertFalse(buffer.lock.locked())

    async def test_close_clears_retired_channels_and_wakes_all_polling_senders(self):
        old_channel = FakeChannel(buffered=8, state="closed")
        channels = [FakeChannel(str(index)) for index in range(2)]
        buffer = self.buffer(channels)
        buffer.retire(old_channel)
        tasks = [self.sending(buffer, channel) for channel in channels]
        await asyncio.sleep(0)
        buffer.close()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), .5)
        self.assertTrue(all(isinstance(result, ConnectionError) for result in results))
        self.assertFalse(any(channel.sent for channel in channels))
        self.assertFalse(buffer.retired)
        self.assertFalse(buffer.lock.locked())
        buffer.retire(old_channel)
        buffer.close()
        self.assertFalse(buffer.retired)

    async def test_cancel_bridge_while_gated_returns_delivery_credit_and_frees_next_sender(self):
        peer = self.peer()
        peer.control = FakeChannel(buffered=16)
        reader = FakeReader(b"\x00cancel me\xff")
        writer = FakeWriter()
        channel = FakeChannel()
        bridge = core.StreamBridge(peer, channel, reader=reader, writer=writer)
        other_channel = FakeChannel()
        other_bridge = core.StreamBridge(peer, other_channel)
        peer.streams.update((bridge, other_bridge))
        bridge.task = asyncio.create_task(bridge.run())
        self.tasks.append(bridge.task)
        await asyncio.wait_for(reader.read_event.wait(), 1)
        self.assertEqual(peer.delivery.held[bridge], len(b"\x00cancel me\xff"))
        self.assertFalse(bridge.pending_times)
        self.assertFalse(channel.sent)
        surviving = self.sending(peer.send_buffer, other_channel)
        await asyncio.sleep(0)
        bridge.task.cancel()
        await asyncio.wait_for(bridge.task, 1)
        self.assertTrue(bridge.closed)
        self.assertTrue(writer.closed)
        self.assertNotIn(bridge, peer.streams)
        self.assertNotIn(bridge, peer.delivery.held)
        self.assertEqual(peer.delivery.used, 0)
        self.assertFalse(surviving.done())
        peer.control.bufferedAmount = 0
        peer.send_buffer.changed.set()
        await asyncio.wait_for(surviving, 1)
        self.assertEqual(other_channel.sent, [b"data"])
        self.assertFalse(peer.send_buffer.lock.locked())
        self.assertFalse(peer.send_buffer.retired)


if __name__ == "__main__":
    unittest.main()
