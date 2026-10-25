"""Bound queued DataChannel data independently of bytes already in flight."""
import asyncio
import time


class SendBuffer:
    def __init__(self, buffered_bytes, limit=16384, clock=time.perf_counter):
        self.buffered_bytes = buffered_bytes
        self.limit = limit
        self.clock = clock
        self.changed = asyncio.Event()
        self.lock = asyncio.Lock()
        self.closed = False
        self.retired = set()

    def retire(self, channel):
        # SCTP may retain an aborted channel's application queue after its
        # stream is removed. Keep charging those bytes until they drain.
        if not self.closed:
            self.retired_bytes()
            if channel.bufferedAmount:
                self.retired.add(channel)
        self.changed.set()

    def retired_bytes(self):
        self.retired = {channel for channel in self.retired if channel.bufferedAmount}
        return sum(channel.bufferedAmount for channel in self.retired
                   if channel.readyState != "open")

    async def send(self, channel, data):
        if len(data) > self.limit:
            raise ValueError("O bloco excede o limite de envio.")
        # asyncio.Lock admits waiting senders in order. Keep the check and
        # synchronous channel.send together so concurrent streams cannot all
        # consume the same available buffer space.
        async with self.lock:
            while not self.closed:
                if channel.readyState != "open":
                    raise ConnectionError("O fluxo foi encerrado.")
                self.changed.clear()
                retired = self.retired_bytes()
                if self.buffered_bytes() + retired + len(data) <= self.limit:
                    sent_at = self.clock()
                    channel.send(data)
                    return sent_at
                if self.retired:
                    # aiortc removes event listeners on final channel close.
                    # Recheck only this exceptional drain without relying on
                    # a notification the closed channel can no longer emit.
                    try:
                        await asyncio.wait_for(self.changed.wait(), .05)
                    except TimeoutError:
                        pass
                else:
                    await self.changed.wait()
            raise ConnectionError("O túnel foi encerrado.")

    def close(self):
        self.closed = True
        self.retired.clear()
        self.changed.set()
