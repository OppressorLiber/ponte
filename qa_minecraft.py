"""Read-only status query to a real LAN world, then the same query via Ponte."""
import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import struct
import time

from pontelan.core import TunnelService
from pontelan.discovery import discover
from qa_public import varint, read_varint


async def query(port):
    reader, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', port), 5)
    try:
        address = b'127.0.0.1'
        handshake = b'\x00' + varint(-1) + varint(len(address)) + address + struct.pack('!H', port) + b'\x01'
        writer.write(varint(len(handshake)) + handshake + b'\x01\x00')
        await writer.drain()
        length = await asyncio.wait_for(read_varint(reader), 8)
        if not 1 <= length <= 2 * 1024 * 1024:
            raise ValueError('Resposta de status fora do limite')
        packet = await asyncio.wait_for(reader.readexactly(length), 8)
        if packet[0] != 0:
            raise ValueError('Pacote não é um status Minecraft')
        status = json.loads(packet[packet.index(b'{'):])
        ping = b'\x01' + struct.pack('!q', time.time_ns() // 1000000)
        started = time.monotonic()
        writer.write(varint(len(ping)) + ping)
        await writer.drain()
        pong_length = await asyncio.wait_for(read_varint(reader), 8)
        pong = await asyncio.wait_for(reader.readexactly(pong_length), 8)
        if pong != ping:
            raise ValueError('Resposta ping/pong inconsistente')
        return status, round((time.monotonic() - started) * 1000, 2)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def main(port):
    if not port:
        worlds = await discover(5)
        if not worlds:
            raise RuntimeError('Abra o mundo para LAN ou informe --port com a porta do chat.')
        port = worlds[0]['port']
    direct, direct_rtt = await query(port)
    events = []
    host, guest = TunnelService(lambda e: None), TunnelService(events.append)
    try:
        code = await host.host(port, 'Teste local Minecraft')
        await guest.join(code)
        async with asyncio.timeout(60):
            while not any(e['type'] == 'listening' for e in events):
                await asyncio.sleep(.05)
        tunneled, tunnel_rtt = await query(guest.local_port)
        assert direct['version'] == tunneled['version']
        assert direct['description'] == tunneled['description']
        result = {'passed': True, 'scope': 'read-only status+ping of REAL Minecraft through same-PC P2P; no player login',
                  'version': tunneled.get('version'), 'players_online': tunneled.get('players', {}).get('online'),
                  'route': guest.peers['host'].route(), 'status_ping_ms_same_pc': tunnel_rtt,
                  'direct_status_ping_ms_same_pc': direct_rtt,
                  'status_bytes_sent': guest.sent, 'status_bytes_received': guest.received,
                  'forge_status_present': 'forgeData' in tunneled or 'modinfo' in tunneled,
                  'save_modified': False, 'remote_house_test': False}
        Path('qa-minecraft.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=True))
    finally:
        await guest.stop()
        await host.stop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main(args.port))
