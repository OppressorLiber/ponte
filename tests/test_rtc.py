"""Real aiortc shutdown while ICE work is queued or still negotiating."""
import asyncio
import unittest
from unittest.mock import patch

from aiortc import RTCConfiguration, RTCPeerConnection

from pontelan.rtc import RoomPeerConnection


class RTCShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_immediate_close_after_answer_has_no_background_errors_or_live_checks(self):
        errors = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()

        def record(loop, context):
            errors.append(context)
            if previous:
                previous(loop, context)
            else:
                loop.default_exception_handler(context)

        loop.set_exception_handler(record)
        self.addCleanup(loop.set_exception_handler, previous)
        for _ in range(4):
            left = RoomPeerConnection(RTCConfiguration(iceServers=[]))
            right = RoomPeerConnection(RTCConfiguration(iceServers=[]))
            try:
                left.createDataChannel("control")
                await left.setLocalDescription(await left.createOffer())
                await right.setRemoteDescription(left.localDescription)
                await right.setLocalDescription(await right.createAnswer())
                await left.setRemoteDescription(right.localDescription)
            finally:
                await asyncio.gather(left.close(), right.close())
            for peer in (left, right):
                self.assertFalse(peer._room_connect_tasks)
                self.assertTrue(all(task.done() for task in peer._room_checks()))
                self.assertEqual(peer.connectionState, "closed")
            await asyncio.sleep(.03)
        self.assertEqual(errors, [])

    async def test_queued_connect_does_not_start_after_revocation(self):
        peer = RoomPeerConnection(RTCConfiguration(iceServers=[]))
        with patch.object(RTCPeerConnection, "_RTCPeerConnection__connect") as connect:
            queued = asyncio.create_task(peer._RTCPeerConnection__connect())
            await peer.close()
            await queued
            connect.assert_not_called()

    async def test_cancelled_close_caller_does_not_cancel_transport_cleanup(self):
        peer = RoomPeerConnection(RTCConfiguration(iceServers=[]))
        entered, release = asyncio.Event(), asyncio.Event()
        original = RTCPeerConnection.close
        count = 0

        async def gated(connection):
            nonlocal count
            count += 1
            entered.set()
            await release.wait()
            await original(connection)

        with patch.object(RTCPeerConnection, "close", gated):
            closing = asyncio.create_task(peer.close())
            await asyncio.wait_for(entered.wait(), 1)
            closing.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await closing
            release.set()
            await asyncio.wait_for(peer.close(), 1)
        self.assertEqual(count, 1)
        self.assertEqual(peer.connectionState, "closed")
