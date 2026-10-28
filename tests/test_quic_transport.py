"""Real QUIC and ICE on loopback, including authentication and loss recovery."""
import asyncio
import json
import struct
import unittest
from unittest.mock import patch

from pontelan.quic_transport import (
    CHANNEL_WINDOW, CONNECTION_WINDOW, MAX_CHANNELS, MAX_DATAGRAM_QUEUE,
    PREFIX, QuicPeerConnection, validate_description,
)


async def eventually(predicate, timeout=4):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.005)


def alter(description, change):
    value = json.loads(description[len(PREFIX):])
    change(value)
    return PREFIX + json.dumps(value)


class QuicTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.peers = []
        self.hosts = patch("aioice.ice.get_host_addresses", return_value=["127.0.0.1"])
        self.hosts.start()
        self.addCleanup(self.hosts.stop)

    def peer(self):
        peer = QuicPeerConnection(stun_server=None)
        self.peers.append(peer)
        return peer

    async def asyncTearDown(self):
        await asyncio.gather(*(peer.close() for peer in self.peers))

    async def connected(self):
        left, right = self.peer(), self.peer()
        channel = left.createDataChannel("control")
        offer = await left.offer()
        answer = await right.answer(offer)
        await left.accept_answer(answer)
        await eventually(lambda: left.connectionState == right.connectionState == "connected")
        return left, right, channel

    async def test_real_ice_mutual_tls_channels_and_datagrams(self):
        left, right = self.peer(), self.peer()
        left.extensions = {"ports": [{"id": "game", "port": 25565}]}
        right.extensions = {"room": "demo"}
        outgoing = left.createDataChannel("control")
        self.assertEqual(outgoing.readyState, "connecting")
        incoming, messages = [], []

        def received(channel):
            incoming.append(channel)
            channel.on("message", lambda message: (messages.append(message), channel.send(message)))

        right.on("datachannel", received)
        replies = []
        outgoing.on("message", replies.append)
        offer = await left.offer()
        answer = await right.answer(offer)
        await left.accept_answer(answer)
        await eventually(lambda: outgoing.readyState == "open" and len(incoming) == 1)
        outgoing.send(b"\x00binary\x00")
        outgoing.send('açúcar')
        await eventually(lambda: len(replies) == 2)
        self.assertEqual(messages, [b"\x00binary\x00", 'açúcar'])
        self.assertEqual(messages, replies)
        self.assertEqual(left.remote_extensions, {"room": "demo"})
        self.assertEqual(right.remote_extensions["ports"][0]["port"], 25565)
        datagrams = []
        right.on("datagram", datagrams.append)
        self.assertTrue(left.send_datagram(b"voice"))
        await eventually(lambda: datagrams == [b"voice"])
        self.assertIn("host ↔ host", left.selected_route())
        self.assertIn("QUIC", left.selected_route())
        self.assertIsNotNone(left.diagnostics()["rtt_ms"])
        self.assertNotIn("127.0.0.1", json.dumps(left.diagnostics()))
        self.assertTrue(right._quic.tls._request_client_certificate)
        self.assertIsNotNone(right._quic.tls._peer_certificate)

    async def test_lost_packet_retransmitted_and_independent_stream_delivered(self):
        left, right, channel = await self.connected()
        received = {}
        right.on("datachannel", lambda remote: remote.on("message", lambda data: received.setdefault(remote.label, []).append(data)))
        # The first header might already be delivered; attach to that channel too.
        for remote in right._channels:
            remote.on("message", lambda data: received.setdefault("control", []).append(data))
        await asyncio.sleep(.04)
        original = left._ice.send
        dropped = []

        async def drop_one(packet):
            if not dropped:
                dropped.append(packet)
                return
            await original(packet)

        left._ice.send = drop_one
        data = b"retransmit" * 1000
        channel.send(data)
        other = left.createDataChannel("second")
        other.send(b"independent")
        await eventually(lambda: received.get("control") == [data] and received.get("second") == [b"independent"])
        self.assertEqual(len(dropped), 1)
        await eventually(lambda: channel.bufferedAmount == 0)

    async def test_datagrams_lost_on_wire_are_not_retransmitted(self):
        left, right, _ = await self.connected()
        datagrams = []
        right.on("datagram", datagrams.append)
        await asyncio.sleep(.06)
        original = left._ice.send
        dropped = []

        async def discard(packet):
            dropped.append(packet)

        left._ice.send = discard
        self.assertTrue(left.send_datagram(b"discard-this-voice-frame"))
        await eventually(lambda: bool(dropped))
        await asyncio.sleep(.04)
        left._ice.send = original
        await asyncio.sleep(.3)
        self.assertEqual(datagrams, [])
        self.assertTrue(left.send_datagram(b"next-voice-frame"))
        await eventually(lambda: datagrams == [b"next-voice-frame"])

    async def test_unsent_drains_but_unacked_memory_stays_accounted_and_bounded(self):
        left, right, channel = await self.connected()
        await asyncio.sleep(.05)
        original = right._ice.send

        async def withhold_ack(packet):
            return

        right._ice.send = withhold_ack
        notifications = []
        channel.on("bufferedamountlow", lambda: notifications.append(True))
        channel.send(b"initial" * 700)
        await eventually(lambda: channel.bufferedAmount == 0)
        self.assertGreater(channel.unackedAmount, 0)
        self.assertGreater(left.unackedAmount, 0)
        payload = b"x" * 16000
        while channel.unackedAmount + len(payload) + 5 <= CHANNEL_WINDOW:
            channel.send(payload)
        self.assertGreater(channel.unackedAmount, 250000)
        with self.assertRaises(BufferError):
            channel.send(payload)
        await asyncio.sleep(.03)
        self.assertGreater(channel.bufferedAmount, 0)
        self.assertLessEqual(channel.unackedAmount, CHANNEL_WINDOW)
        self.assertLessEqual(left.unackedAmount, CONNECTION_WINDOW)
        right._ice.send = original
        await eventually(lambda: channel.bufferedAmount == 0)
        await eventually(lambda: channel.unackedAmount == 0)
        self.assertTrue(notifications)

    async def test_both_sides_reject_unpinned_certificate_before_application_data(self):
        for wrong_side in ("client", "server"):
            with self.subTest(wrong_side=wrong_side):
                left, right = self.peer(), self.peer()
                left.createDataChannel("control")
                connected, incoming = [], []
                rejecting = right if wrong_side == "client" else left
                rejecting.on("connectionstatechange", lambda p=rejecting: connected.append(p.connectionState))
                rejecting.on("datachannel", incoming.append)
                offer = await left.offer()
                if wrong_side == "client":
                    offer = alter(offer, lambda data: data.update(certificate_sha256="0" * 64))
                answer = await right.answer(offer)
                if wrong_side == "server":
                    answer = alter(answer, lambda data: data.update(certificate_sha256="0" * 64))
                await left.accept_answer(answer)
                await eventually(lambda: rejecting.connectionState == "failed")
                self.assertNotIn("connected", connected)
                self.assertFalse(incoming)
                self.assertFalse(rejecting._authenticated)
                await asyncio.gather(left.close(), right.close())

    async def test_invalid_frame_resets_only_offending_stream(self):
        left, right, channel = await self.connected()
        opened = []
        right.on("datachannel", opened.append)
        await eventually(lambda: bool(right._channels))
        left._quic.send_stream_data(channel._stream_id, struct.pack("!BI", 0, 0xffffffff))
        left._wakeup.set()
        await eventually(lambda: channel.readyState == "closed")
        self.assertEqual(left.connectionState, "connected")
        good = left.createDataChannel("good")
        good.send(b"still-alive")
        await eventually(lambda: any(c.label == "good" for c in opened))
        self.assertEqual(right.connectionState, "connected")

    async def test_channel_and_datagram_limits_do_not_create_unbounded_queue(self):
        left, right, _ = await self.connected()
        for index in range(MAX_CHANNELS - 1):
            left.createDataChannel(f"channel-{index}")
        with self.assertRaises(BufferError):
            left.createDataChannel("overflow")
        accepted = sum(left.send_datagram(b"x") for _ in range(1000))
        self.assertLessEqual(accepted, MAX_DATAGRAM_QUEUE)
        self.assertLessEqual(len(left._quic._datagrams_pending), MAX_DATAGRAM_QUEUE)
        self.assertGreater(left.diagnostics()["datagrams_dropped"], 0)
        with self.assertRaises(ValueError):
            left.send_datagram(b"x" * 1001)

    async def test_close_during_ice_and_gather_owns_all_tasks(self):
        errors = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: errors.append(context))
        self.addCleanup(loop.set_exception_handler, previous)
        for mode in ("gather", "ice"):
            left, right = self.peer(), self.peer()
            left.createDataChannel("control")
            if mode == "gather":
                gathering = asyncio.create_task(left.offer())
                await asyncio.sleep(0)
                await left.close()
                await asyncio.gather(gathering, return_exceptions=True)
            else:
                answer = await right.answer(await left.offer())
                await left.accept_answer(answer)
                await asyncio.gather(left.close(), right.close())
            self.assertEqual(left.connectionState, "closed")
            if left._run_task is not None:
                self.assertTrue(left._run_task.done())
            if left._ice is not None:
                self.assertFalse(left._ice._protocols)
                self.assertTrue(all(pair.task is None or pair.task.done() for pair in left._ice._check_list))
        await asyncio.sleep(.03)
        self.assertEqual(errors, [])

    async def test_cancelled_close_caller_does_not_cancel_cleanup(self):
        left, _, _ = await self.connected()
        entered, release = asyncio.Event(), asyncio.Event()
        original = left._ice.close

        async def gated():
            entered.set()
            await release.wait()
            await original()

        left._ice.close = gated
        closing = asyncio.create_task(left.close())
        await asyncio.wait_for(entered.wait(), 1)
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        release.set()
        await asyncio.wait_for(left.close(), 1)
        self.assertEqual(left.connectionState, "closed")
        self.assertFalse(left._ice._protocols)

    async def test_missing_tls_client_certificate_cannot_open_application_stream(self):
        left, right = self.peer(), self.peer()
        left.createDataChannel("control")
        opened = []
        right.on("datachannel", opened.append)
        offer = await left.offer()
        answer = await right.answer(offer)
        # The advertised fingerprint is still the real offer certificate, but
        # this client declines CertificateRequest instead of proving possession.
        original = left._configuration

        def no_client_certificate():
            configuration = original()
            configuration.certificate = None
            configuration.private_key = None
            return configuration

        left._configuration = no_client_certificate
        await left.accept_answer(answer)
        await eventually(lambda: right.connectionState == "failed")
        self.assertFalse(right._authenticated)
        self.assertFalse(opened)

    async def test_close_flushes_all_frames_before_fin_and_releases_native_buffers(self):
        left, right = self.peer(), self.peer()
        channel = left.createDataChannel("control")
        messages, closed = [], []

        def remote_channel(remote):
            remote.on("message", messages.append)
            remote.on("close", lambda: closed.append(True))

        right.on("datachannel", remote_channel)
        await left.accept_answer(await right.answer(await left.offer()))
        await eventually(lambda: channel.readyState == "open")
        for index in range(12):
            channel.send(bytes([index]) * 10000)
        channel.close()
        await eventually(lambda: len(messages) == 12 and closed and channel.readyState == "closed")
        self.assertEqual(messages, [bytes([index]) * 10000 for index in range(12)])
        await eventually(lambda: left.unackedAmount == right.unackedAmount == 0)
        self.assertEqual(left.connectionState, "connected")

    async def test_retired_stream_buffers_count_toward_global_memory_limit(self):
        left, right, channel = await self.connected()
        await asyncio.sleep(.05)
        original = right._ice.send

        async def no_ack(packet):
            return

        right._ice.send = no_ack
        channel.send(b"x" * 16000)
        channel._abort()
        self.assertEqual(channel.readyState, "closed")
        self.assertNotIn(channel, left._channels)
        self.assertGreaterEqual(left.unackedAmount, 16000)
        right._ice.send = original
        await eventually(lambda: left.unackedAmount == 0)

    async def test_native_stream_credit_allows_repeated_open_close_beyond_initial_limit(self):
        left, right, _ = await self.connected()
        messages = []
        right.on("datachannel", lambda channel: channel.on("message", messages.append))
        for index in range(70):
            channel = left.createDataChannel(f"flow-{index}")
            channel.send(str(index))
            channel.close()
            await eventually(lambda: channel.readyState == "closed")
        self.assertEqual(messages, [str(index) for index in range(70)])
        self.assertEqual(left.connectionState, "connected")
        self.assertEqual(right.connectionState, "connected")

    async def test_sparse_native_receive_offsets_do_not_increase_credit(self):
        from aioquic.quic.packet import QuicStreamFrame
        left, right, channel = await self.connected()
        await eventually(lambda: bool(right._channels))
        stream = right._quic._streams[channel._stream_id]
        start = stream.receiver._buffer_start
        # Exercise the native sparse reassembler without manufacturing QUIC
        # packets: missing prefix must withhold further MAX_STREAM_DATA credit.
        stream.receiver.handle_frame(QuicStreamFrame(offset=start + CHANNEL_WINDOW - 20, data=b"sparse"))
        right._quic._local_max_data.used += CHANNEL_WINDOW - 14
        right._wakeup.set()
        await asyncio.sleep(.03)
        self.assertLessEqual(stream.max_stream_data_local - stream.receiver._buffer_start, CHANNEL_WINDOW)
        retained = sum(max(0, item.receiver.highest_offset - item.receiver._buffer_start)
                       for item in right._quic._streams.values())
        self.assertLessEqual(right._quic._local_max_data.value - right._quic._local_max_data.used + retained,
                             CONNECTION_WINDOW)

    async def test_description_validation_rejects_relay_hostname_tcp_and_oversize(self):
        peer = self.peer()
        description = await peer.offer()
        validate_description(description)
        base = json.loads(description[len(PREFIX):])
        for candidate in (
            "a 1 udp 1 127.0.0.1 1234 typ relay",
            "a 1 tcp 1 127.0.0.1 1234 typ host",
            "a 1 udp 1 example.com 1234 typ host",
            "a 2 udp 1 127.0.0.1 1234 typ host",
            "a 1 udp 1 0.0.0.0 1234 typ host",
            "a 1 udp 1 224.0.0.1 1234 typ host",
            "a 1 udp 1 127.0.0.1 70000 typ host",
            "a 1 udp 1 127.0.0.1 1234 wrong host",
            "a 1 udp 1 127.0.0.1 1234 typ host rport",
            "a 1 udp 1 127.0.0.1 1234 typ host generation 0 generation 1",
            "a 1 udp 1 127.0.0.1 1234 typ host raddr 192.168.1.1.local",
        ):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                validate_description(alter(description, lambda data: data["ice"].update(candidates=[candidate])))
        for invalid in ("", "v=0", PREFIX + "{}", description + "x", PREFIX + "x" * 128000,
                        alter(description, lambda data: data["ice"].update(candidates=base["ice"]["candidates"] * 49)),
                        alter(description, lambda data: data.update(extensions={"x": "x" * 9000}))):
            with self.assertRaises(ValueError):
                validate_description(invalid)
        with self.assertRaises(ValueError):
            validate_description(alter(description, lambda data: data.update(v=True)))
