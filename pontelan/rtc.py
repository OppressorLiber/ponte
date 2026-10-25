"""Own aiortc's background negotiation tasks during room revocation.

aiortc 1.15.0 (pinned in requirements.txt) schedules its private __connect
coroutine without retaining the task, and aioice.close leaves candidate checks
running until connect consumes ICE_FAILED. Closing during negotiation can then
send checks on closed UDP transports. Keep these version-specific details here;
the RTC transport and all established data-channel behavior stay unchanged.
"""
from __future__ import annotations

import asyncio

from aiortc import RTCPeerConnection


class RoomPeerConnection(RTCPeerConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._room_closing = False
        self._room_connect_tasks = set()
        self._room_close_task = None

    async def _RTCPeerConnection__connect(self):
        # Also cover a coroutine which was queued by setRemoteDescription but
        # has not started when close() revokes this participant.
        if self._room_closing:
            return
        task = asyncio.current_task()
        self._room_connect_tasks.add(task)
        try:
            await super()._RTCPeerConnection__connect()
        finally:
            self._room_connect_tasks.discard(task)

    def _room_checks(self):
        return {pair.task for transport in self._RTCPeerConnection__iceTransports
                for pair in transport._connection._check_list if pair.task is not None}

    @staticmethod
    async def _cancel(tasks):
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _room_close(self):
        await self._cancel(list(self._room_connect_tasks))
        await self._cancel(self._room_checks())
        await super().close()
        # An incoming connectivity check may have queued a triggered check
        # while transports were stopping. Retrieve and retire that task too.
        await self._cancel(self._room_checks())

    async def close(self):
        if self._room_close_task is None:
            self._room_closing = True
            self._room_close_task = asyncio.create_task(self._room_close())
        # A cancelled approval must not cancel transport teardown midway.
        await asyncio.shield(self._room_close_task)
