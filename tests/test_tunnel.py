import asyncio
import contextlib
import hashlib
import json
import os
import time
import unittest
import uuid

from aiohttp import web

from pontelan.core import TunnelService, direct_description
from pontelan.discovery import parse_announcement

if os.name == 'nt':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class LocalNtfy:
    """HTTP streaming fixture; core still executes real ICE/DTLS/SCTP and TCP."""
    def __init__(self):
        self.subscribers = {}
        self.history = {}

    async def start(self):
        app = web.Application()
        app.router.add_get('/{topic}/json', self.subscribe)
        app.router.add_post('/{topic}', self.publish)
        self.runner = web.AppRunner(app, shutdown_timeout=.5)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'

    async def subscribe(self, request):
        topic = request.match_info['topic']
        response = web.StreamResponse(headers={'Content-Type': 'application/x-ndjson'})
        await response.prepare(request)
        queue = asyncio.Queue()
        self.subscribers.setdefault(topic, []).append(queue)
        try:
            await response.write((json.dumps({'event': 'open', 'id': uuid.uuid4().hex})+'\n').encode())
            for item in self.history.get(topic, []):
                await response.write((json.dumps(item)+'\n').encode())
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), .5)
                except asyncio.TimeoutError:
                    item = {'event': 'keepalive'}
                await response.write((json.dumps(item)+'\n').encode())
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.subscribers[topic].remove(queue)
        return response

    async def publish(self, request):
        topic = request.match_info['topic']
        data = await request.text()
        if len(data.encode()) > 4096:
            return web.Response(status=413)
        item = {'event': 'message', 'id': uuid.uuid4().hex, 'time': int(time.time()), 'message': data}
        self.history.setdefault(topic, []).append(item)
        for queue in self.subscribers.get(topic, []):
            await queue.put(item)
        return web.json_response(item)

    async def close(self):
        await self.runner.cleanup()


async def wait_event(events, predicate, timeout=15):
    async with asyncio.timeout(timeout):
        while True:
            for event in events:
                if predicate(event):
                    return event
            await asyncio.sleep(.02)


class TunnelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.signaling = LocalNtfy()
        await self.signaling.start()
        self.host_events, self.guest_events = [], []
        self.host = TunnelService(self.host_events.append, endpoint=self.signaling.url, stun=False)
        self.guest = TunnelService(self.guest_events.append, endpoint=self.signaling.url, stun=False)
        self.game = await asyncio.start_server(self.game_handler, '127.0.0.1', 0)
        self.game_port = self.game.sockets[0].getsockname()[1]
        self.game_tasks = set()

    async def game_handler(self, reader, writer):
        task = asyncio.current_task()
        self.game_tasks.add(task)
        try:
            request = await reader.read()
            if request:
                writer.write(b'PONTE-RESPONSE\x00' + request[::-1])
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            self.game_tasks.discard(task)

    async def asyncTearDown(self):
        await self.guest.stop()
        await self.host.stop()
        self.game.close()
        await self.game.wait_closed()
        for task in list(self.game_tasks):
            task.cancel()
        await asyncio.gather(*self.game_tasks, return_exceptions=True)
        await self.signaling.close()

    async def connect(self):
        code = await self.host.host(self.game_port, 'Mundo modpack')
        await self.guest.join(code)
        event = await wait_event(self.guest_events, lambda e: e['type'] == 'listening')
        self.assertTrue(event['address'].startswith('127.0.0.1:'))
        return int(event['address'].split(':')[1])

    async def exchange(self, port, payload):
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        writer.write(payload)
        await writer.drain()
        writer.write_eof()
        result = await asyncio.wait_for(reader.read(), 30)
        writer.close()
        await writer.wait_closed()
        self.assertEqual(result, b'PONTE-RESPONSE\x00' + payload[::-1])
        return hashlib.sha256(result).hexdigest()

    async def test_large_bytes_half_close_and_route(self):
        port = await self.connect()
        await self.exchange(port, os.urandom(1500000))
        self.assertGreaterEqual(self.host.received, 1500000)
        self.assertGreaterEqual(self.guest.received, 1500000)
        await wait_event(self.guest_events, lambda e: e.get('rtt_ms') is not None)
        self.assertEqual(self.guest.peers['host'].route(), 'host ↔ host')

    async def test_parallel_game_connections_and_stop(self):
        port = await self.connect()
        await asyncio.gather(*(self.exchange(port, os.urandom(100000+i*13)) for i in range(8)))
        await self.guest.stop()
        with self.assertRaises(OSError):
            await asyncio.open_connection('127.0.0.1', port)
        self.assertEqual(len(self.guest.peers), 0)

    async def test_two_friends_and_invalid_host_port(self):
        with self.assertRaises(ValueError):
            await self.host.host(0)
        port = await self.connect()
        other_events = []
        other = TunnelService(other_events.append, endpoint=self.signaling.url, stun=False)
        try:
            await other.join(self.host.code)
            event = await wait_event(other_events, lambda e: e['type'] == 'listening')
            other_port = int(event['address'].split(':')[1])
            await asyncio.gather(self.exchange(port, b'first'), self.exchange(other_port, b'second'))
            self.assertEqual(len(self.host.peers), 2)
        finally:
            await other.stop()

    async def test_host_disconnect_releases_guest_listener(self):
        port = await self.connect()
        await self.host.stop()
        await wait_event(self.guest_events, lambda e: e.get('state') == 'stopped', timeout=8)
        with self.assertRaises(OSError):
            await asyncio.open_connection('127.0.0.1', port)


class ProtocolTests(unittest.TestCase):
    def test_relay_is_rejected(self):
        with self.assertRaises(ValueError):
            direct_description('v=0\r\na=candidate:1 1 udp 1 192.0.2.1 1234 typ relay\r\n')

    def test_lan_port_parse(self):
        self.assertEqual(parse_announcement(b'[MOTD]Meu mundo[/MOTD][AD]54321[/AD]'),
                         {'name':'Meu mundo', 'port':54321})
        self.assertIsNone(parse_announcement(b'[AD]99999[/AD]'))
        self.assertIsNone(parse_announcement(b'invalid'))


if __name__ == '__main__':
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    unittest.main()
