"""Listen briefly for Minecraft Java LAN announcements; never scan the network."""
import asyncio
import contextlib
import re
import socket
import struct


def parse_announcement(data):
    text = data.decode("utf-8", errors="replace")
    port = re.search(r"\[AD\]([0-9]{1,5})\[/AD\]", text)
    motd = re.search(r"\[MOTD\](.*?)\[/MOTD\]", text, re.DOTALL)
    if not port or not 1 <= int(port[1]) <= 65535:
        return None
    name = re.sub(r"§.", "", motd[1] if motd else "Mundo Minecraft")
    return {"name": name[:60], "port": int(port[1])}


async def discover(seconds=3):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    worlds = {}
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", 4445))
        membership = struct.pack("=4s4s", socket.inet_aton("224.0.2.60"), socket.inet_aton("0.0.0.0"))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        sock.setblocking(False)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while loop.time() < deadline:
            try:
                raw, _ = await asyncio.wait_for(loop.sock_recvfrom(sock, 4096), deadline - loop.time())
            except asyncio.TimeoutError:
                break
            world = parse_announcement(raw)
            if world:
                # Only expose worlds whose TCP port is on THIS PC.
                try:
                    _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", world["port"]), .3)
                    writer.close()
                    with contextlib.suppress(OSError):
                        await writer.wait_closed()
                    worlds[world["port"]] = world
                except (OSError, asyncio.TimeoutError):
                    continue
    except OSError:
        return []
    finally:
        sock.close()
    return list(worlds.values())
