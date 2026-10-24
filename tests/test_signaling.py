from __future__ import annotations

import asyncio
import base64
import json
import secrets
import time
import unittest
from unittest.mock import patch
import uuid
import zlib

from aiohttp import web

from pontelan.signaling import (
    MAX_JSON_BYTES, MAX_REPLAYS, SignalBus, _CONTEXT, _b64encode, new_code, normalize_code,
)


async def ignore_message(message):
    pass


def buses():
    code = new_code()
    guest_id = uuid.uuid4().hex
    return SignalBus(code, "host", True, ignore_message), SignalBus(code, guest_id, False, ignore_message)


class EnvelopeTests(unittest.TestCase):
    def test_code_roundtrip_and_noncanonical_rejection(self):
        code = new_code()
        self.assertEqual(len(code), 49)
        self.assertEqual(normalize_code(" \n" + code + "\n"), code)
        for invalid in ["", "PONTE-abc", code.lower(), code + "=", code[:6] + "A" * 42 + "B", 42]:
            with self.subTest(invalid_type=type(invalid).__name__):
                with self.assertRaises(RuntimeError):
                    normalize_code(invalid)

    def test_authenticated_compressed_roundtrip_and_replay(self):
        host, guest = buses()
        message = {"type": "offer", "sdp": "v=0\r\na=candidate:secret-address\r\n" * 20, "name": "São Paulo"}
        wire = host._encode(guest.peer_id, message)
        self.assertLessEqual(len(wire), 4096)
        self.assertNotIn("candidate", wire)
        self.assertNotIn("São Paulo", wire)
        self.assertEqual(guest._decode(wire), {**message, "_sender": "host"})
        self.assertIsNone(guest._decode(wire))
        response = guest._encode("host", {"type": "answer", "sdp": "ok"})
        self.assertEqual(host._decode(response), {"type": "answer", "sdp": "ok", "_sender": guest.peer_id})

    def test_topics_are_independent_of_encryption_key_and_bounded(self):
        host, guest = buses()
        self.assertEqual(host._topic("host"), guest._topic("host"))
        self.assertNotEqual(host._topic("host"), host._topic(guest.peer_id))
        self.assertLessEqual(len(host._topic("host")), 64)
        self.assertNotIn(host.code[6:], host._topic("host"))
        wire = host._encode(guest.peer_id, {"type": "hello"})
        other = SignalBus(new_code(), guest.peer_id, False, ignore_message)
        self.assertIsNone(other._decode(wire))
        same_invite_wrong_target = SignalBus(host.code, uuid.uuid4().hex, False, ignore_message)
        self.assertIsNone(same_invite_wrong_target._decode(wire))

    def test_tamper_wrong_type_and_invalid_base64_are_ignored(self):
        host, guest = buses()
        wire = host._encode(guest.peer_id, {"type": "hello"})
        packed = bytearray(base64.urlsafe_b64decode(wire[3:] + "=" * (-len(wire[3:]) % 4)))
        packed[-1] ^= 1
        for invalid in ["p1:" + _b64encode(packed), "p1:!" * 40, "x" * 5000, {}, None]:
            self.assertIsNone(guest._decode(invalid))
        for wrong in [{"type": "game", "data": "packet"}, {}, [], {"type": "file"}, {"type": []}]:
            with self.assertRaises(RuntimeError):
                host._encode(guest.peer_id, wrong)

    def test_age_limits_and_replay_cache_are_bounded(self):
        host, guest = buses()
        with patch("pontelan.signaling.time.time", return_value=time.time() - 301):
            expired = host._encode(guest.peer_id, {"type": "hello"})
        self.assertIsNone(guest._decode(expired))
        with patch("pontelan.signaling.time.time", return_value=time.time() + 301):
            future = host._encode(guest.peer_id, {"type": "hello"})
        self.assertIsNone(guest._decode(future))
        for index in range(MAX_REPLAYS + 5):
            wire = host._encode(guest.peer_id, {"type": "hello", "index": index})
            self.assertIsNotNone(guest._decode(wire))
        self.assertEqual(len(guest._seen), MAX_REPLAYS)

    def test_uncompressed_and_provider_limits_are_enforced(self):
        host, guest = buses()
        with self.assertRaisesRegex(RuntimeError, "tamanho permitido"):
            host._encode(guest.peer_id, {"type": "offer", "sdp": "a" * MAX_JSON_BYTES})
        with self.assertRaisesRegex(RuntimeError, "grande demais"):
            host._encode(guest.peer_id, {"type": "offer", "sdp": secrets.token_hex(9000)})
        # Even a correctly authenticated sender cannot force unbounded inflate.
        nonce = secrets.token_bytes(12)
        compressed = zlib.compress(b"a" * (MAX_JSON_BYTES + 1))
        ciphertext = host._cipher.encrypt(nonce, compressed, _CONTEXT + guest.peer_id.encode("ascii"))
        self.assertIsNone(guest._decode("p1:" + _b64encode(nonce + ciphertext)))

    def test_peer_route_and_endpoint_validation(self):
        host, guest = buses()
        with self.assertRaises(RuntimeError):
            host._encode("host", {"type": "hello"})
        with self.assertRaises(RuntimeError):
            guest._encode(uuid.uuid4().hex, {"type": "hello"})
        for peer, is_host in [("x", False), ("host", False), (uuid.uuid4().hex, True)]:
            with self.assertRaises(RuntimeError):
                SignalBus(new_code(), peer, is_host, ignore_message)
        for endpoint in ["http://example.com", "https://user:password@example.com", "https://example.com?q=secret"]:
            with self.assertRaises(RuntimeError):
                SignalBus(new_code(), "host", True, ignore_message, endpoint=endpoint)


class NtfyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.queues = {}
        self.posts = []
        self.since = []
        self.fail_publish = False
        self.app = web.Application()
        self.app.router.add_get("/{topic}/json", self.subscribe)
        self.app.router.add_post("/{topic}", self.publish)
        self.runner = web.AppRunner(self.app, shutdown_timeout=0.1)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.endpoint = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        self.connected = []

    async def asyncTearDown(self):
        for bus in self.connected:
            await bus.close()
        for queues in self.queues.values():
            for queue in queues:
                queue.put_nowait(None)
        await self.runner.cleanup()

    async def subscribe(self, request):
        self.since.append(request.query.get("since"))
        topic = request.match_info["topic"]
        queue = asyncio.Queue()
        self.queues.setdefault(topic, []).append(queue)
        stream = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await stream.prepare(request)
        await stream.write(b'{"event":"open","id":"open0"}\n')
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                await stream.write(json.dumps(event).encode() + b"\n")
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.queues[topic].remove(queue)
        return stream

    async def publish(self, request):
        if self.fail_publish:
            return web.json_response({"error": "rate limited"}, status=429)
        wire = await request.text()
        self.posts.append((request.match_info["topic"], wire))
        event = {"event": "message", "id": uuid.uuid4().hex[:10], "message": wire}
        for queue in self.queues.get(request.match_info["topic"], []):
            # Providers can replay a delivered message on reconnect.
            await queue.put(event)
            await queue.put(event)
        return web.json_response(event)

    async def make_bus(self, code, peer, is_host, inbox):
        async def receive(message):
            await inbox.put(message)
        bus = SignalBus(code, peer, is_host, receive, endpoint=self.endpoint)
        self.connected.append(bus)
        await bus.start()
        return bus

    async def test_open_handshake_bidirectional_encryption_replay_and_close(self):
        code, guest_id = new_code(), uuid.uuid4().hex
        host_inbox, guest_inbox = asyncio.Queue(), asyncio.Queue()
        host = await self.make_bus(code, "host", True, host_inbox)
        guest = await self.make_bus(code, guest_id, False, guest_inbox)
        await guest.send("host", {"type": "hello", "name": "Segredo"})
        self.assertEqual(await asyncio.wait_for(host_inbox.get(), 2), {"type": "hello", "name": "Segredo", "_sender": guest_id})
        await host.send(guest_id, {"type": "offer", "sdp": "v=0\r\nprivateSDP"})
        self.assertEqual((await asyncio.wait_for(guest_inbox.get(), 2))["sdp"], "v=0\r\nprivateSDP")
        await asyncio.sleep(0.03)
        self.assertTrue(host_inbox.empty())
        self.assertTrue(guest_inbox.empty())
        self.assertEqual(len(self.posts), 2)
        self.assertTrue(all("Segredo" not in wire and "privateSDP" not in wire for _, wire in self.posts))
        self.assertTrue(all(len(wire) <= 4096 for _, wire in self.posts))
        self.assertTrue(all(value and value.isdecimal() for value in self.since))
        await host.close()
        await host.close()
        self.assertIsNone(host._session)
        with self.assertRaises(RuntimeError):
            await host.send(guest_id, {"type": "bye"})

    async def test_reconnect_uses_last_message_id(self):
        code, guest_id = new_code(), uuid.uuid4().hex
        inbox = asyncio.Queue()
        host = await self.make_bus(code, "host", True, inbox)
        guest = await self.make_bus(code, guest_id, False, asyncio.Queue())
        await guest.send("host", {"type": "hello"})
        await asyncio.wait_for(inbox.get(), 2)
        expected_cursor = host._cursor
        for queue in list(self.queues[host._topic("host")]):
            await queue.put(None)
        deadline = time.monotonic() + 3
        while expected_cursor not in self.since and time.monotonic() < deadline:
            await asyncio.sleep(0.03)
        self.assertIn(expected_cursor, self.since)

    async def test_rate_limit_is_friendly_and_no_publish_retry(self):
        host = await self.make_bus(new_code(), "host", True, asyncio.Queue())
        self.fail_publish = True
        with self.assertRaisesRegex(RuntimeError, "limite temporário"):
            await host.send(uuid.uuid4().hex, {"type": "offer", "sdp": "test"})
        self.assertEqual(self.posts, [])

    async def test_callback_can_close_its_own_bus_without_deadlock(self):
        code, guest_id = new_code(), uuid.uuid4().hex
        closed = asyncio.Event()
        host = None

        async def receive(message):
            await host.close()
            closed.set()

        host = SignalBus(code, "host", True, receive, endpoint=self.endpoint)
        self.connected.append(host)
        await host.start()
        subscription = host._task
        guest = await self.make_bus(code, guest_id, False, asyncio.Queue())
        await guest.send("host", {"type": "bye"})
        await asyncio.wait_for(closed.wait(), 2)
        await asyncio.wait_for(subscription, 2)
        self.assertIsNone(host._session)


if __name__ == "__main__":
    unittest.main()
