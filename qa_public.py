"""Real public rendezvous and real same-PC P2P. Not a remote Minecraft game."""
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import struct
import time

from pontelan.core import TunnelService


def varint(value):
    result = bytearray()
    value &= 0xffffffff
    while True:
        part = value & 127
        value >>= 7
        result.append(part | (128 if value else 0))
        if not value:
            return bytes(result)


async def read_varint(reader):
    value = 0
    for shift in range(0, 35, 7):
        part = (await reader.readexactly(1))[0]
        value |= (part & 127) << shift
        if part < 128:
            return value
    raise ValueError('VarInt invalid')


async def game_fixture(reader, writer):
    try:
        length = await read_varint(reader)
        await reader.readexactly(length)  # handshake
        length = await read_varint(reader)
        request = await reader.readexactly(length)
        if request != b'\x00':
            raise ValueError('status request invalid')
        status = json.dumps({'version': {'name': 'Ponte protocol fixture', 'protocol': -1},
                             'players': {'max': 8, 'online': 1},
                             'description': {'text': 'Mundo modpack de teste'}}).encode()
        payload = b'\x00' + varint(len(status)) + status
        writer.write(varint(len(payload)) + payload)
        await writer.drain()
        # Then arbitrary bytes both ways, including a TCP half-close.
        data = await reader.read()
        writer.write(hashlib.sha256(data).digest() + data[::-1])
        await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def main():
    host_events, guest_events = [], []
    host, guest = TunnelService(host_events.append), TunnelService(guest_events.append)
    game = await asyncio.start_server(game_fixture, '127.0.0.1', 0)
    port = game.sockets[0].getsockname()[1]
    started = time.monotonic()
    try:
        code = await host.host(port, 'Mundo modpack de teste')
        await guest.join(code)
        async with asyncio.timeout(60):
            while not any(e['type'] == 'listening' for e in guest_events):
                await asyncio.sleep(.05)
        setup_seconds = round(time.monotonic() - started, 3)
        reader, writer = await asyncio.open_connection('127.0.0.1', guest.local_port)
        address = b'127.0.0.1'
        handshake = b'\x00' + varint(-1) + varint(len(address)) + address + struct.pack('!H', guest.local_port) + b'\x01'
        writer.write(varint(len(handshake)) + handshake + b'\x01\x00')
        await writer.drain()
        length = await read_varint(reader)
        packet = await reader.readexactly(length)
        # Status JSON length is a VarInt; locate the first JSON character.
        status = json.loads(packet[packet.index(b'{'):])
        assert status['description']['text'] == 'Mundo modpack de teste'
        payload = os.urandom(3 * 1024 * 1024)
        transfer_started = time.monotonic()
        writer.write(payload)
        await writer.drain()
        writer.write_eof()
        response = await asyncio.wait_for(reader.read(), 60)
        assert response[:32] == hashlib.sha256(payload).digest()
        assert response[32:] == payload[::-1]
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(.2)
        peer = guest.peers['host']
        evidence = {'passed': True, 'signaling': 'https://ntfy.sh (real HTTP streams and encrypted SDP)',
                    'scope': 'two complete app services on THIS PC; not two houses or a real Minecraft game',
                    'setup_seconds': setup_seconds, 'route': peer.route(),
                    'local_candidate_types': sorted({c.type for c in peer.pc.sctp.transport.transport.iceGatherer.getLocalCandidates()}),
                    'payload_bytes': len(payload), 'response_bytes': len(response),
                    'response_sha256': hashlib.sha256(response).hexdigest(),
                    'transfer_seconds': round(time.monotonic() - transfer_started, 3),
                    'rtt_ms_same_pc': peer.rtt_ms, 'minecraft_status_fixture': status['description']['text'],
                    'host': host.diagnostics(), 'guest': guest.diagnostics()}
        Path('qa-public.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({k: v for k, v in evidence.items() if k not in ('host', 'guest')}, ensure_ascii=True))
    finally:
        await guest.stop()
        await host.stop()
        game.close()
        await game.wait_closed()


if __name__ == '__main__':
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
