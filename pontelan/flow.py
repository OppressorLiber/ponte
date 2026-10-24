"""Fair, delay-sensitive admission of TCP bytes to one SCTP association."""
from collections import deque
import asyncio
import contextlib
import math
import time


class DeliveryWindow:
    def __init__(self, initial=65536, *, minimum=16384, maximum=262144,
                 quantum=4096, clock=time.perf_counter):
        self.minimum, self.maximum, self.quantum = minimum, maximum, quantum
        self.limit = max(minimum, min(maximum, initial))
        self.clock = clock
        self.used = 0
        self.held = {}
        self.grants = {}
        self.borrowed = 0
        self.waiters = deque()
        self.base_rtt = None
        self.last_adjust = None
        self.last_rtt = None
        self.adjustments = 0
        self.closed = False

    async def acquire(self, flow, count):
        if self.closed:
            raise ConnectionError("O túnel foi encerrado.")
        ticket = [flow, count, asyncio.get_running_loop().create_future(), None]
        self.waiters.append(ticket)
        self._pump()
        try:
            await ticket[2]
        except BaseException:
            if ticket[3] is not None:
                self._refund(flow, ticket[3])
            else:
                with contextlib.suppress(ValueError):
                    self.waiters.remove(ticket)
            self._pump()
            raise

    def _pump(self):
        if self.closed:
            return
        # Each flow has at most one queued read. Scan once in arrival order so
        # a small request can use space that cannot fit the next bulk chunk.
        for _ in range(len(self.waiters)):
            ticket = self.waiters.popleft()
            flow, count, future, _ = ticket
            if future.cancelled():
                continue
            normal = self.used + count <= self.limit
            # A stalled TCP destination must not hold the entire peer hostage.
            # New flows can borrow one chunk from a bounded startup reserve.
            reserve = max(self.quantum, min(4 * self.quantum, self.limit // 4))
            startup = not self.held.get(flow, 0) and self.borrowed + count <= reserve
            if normal or startup:
                self.used += count
                self.held[flow] = self.held.get(flow, 0) + count
                borrowed = not normal
                grant = [count, borrowed]
                self.grants.setdefault(flow, deque()).append(grant)
                if borrowed:
                    self.borrowed += count
                ticket[3] = grant
                future.set_result(None)
            else:
                self.waiters.append(ticket)

    def _refund(self, flow, grant):
        # Cancellation happens before this acquisition returns to its sender.
        # Return this exact reservation, not an older frame awaiting an ACK.
        grants = self.grants.get(flow, ())
        for index, held_grant in enumerate(grants):
            if held_grant is grant:
                count, borrowed = grant
                del grants[index]
                self.used -= count
                self.held[flow] -= count
                if borrowed:
                    self.borrowed -= count
                if not grants:
                    self.grants.pop(flow, None)
                if not self.held[flow]:
                    self.held.pop(flow, None)
                return

    def release(self, flow, count, rtt=None):
        count = min(count, self.held.get(flow, 0))
        if not count:
            return
        loaded = bool(self.waiters)
        before = self.used
        self.used -= count
        released = count
        grants = self.grants[flow]
        while released:
            grant = grants[0]
            consumed = min(released, grant[0])
            if grant[1]:
                self.borrowed -= consumed
            grant[0] -= consumed
            released -= consumed
            if grant[0] == 0:
                grants.popleft()
        if not grants:
            self.grants.pop(flow, None)
        remaining = self.held[flow] - count
        if remaining:
            self.held[flow] = remaining
        else:
            self.held.pop(flow, None)
        if rtt is not None and math.isfinite(rtt) and rtt > 0:
            now = self.clock()
            self.base_rtt = rtt if self.base_rtt is None else min(self.base_rtt, rtt)
            self.last_rtt = rtt
            epoch = max(.02, self.base_rtt)
            if self.last_adjust is None or now - self.last_adjust >= epoch:
                # Do not mistake normal scheduling/jitter for a growing queue.
                # Scale the delay target with propagation time: a long path
                # needs more bytes in flight than a nearby one. Keep a 40 ms
                # floor so ordinary scheduler jitter does not collapse it.
                delay_target = max(.04, self.base_rtt)
                queued = before * max(0, rtt - self.base_rtt - delay_target) / rtt
                old = self.limit
                if queued > 2 * self.quantum:
                    self.limit = max(self.minimum, (int(self.limit * .8) // self.quantum) * self.quantum)
                elif loaded and queued < self.quantum and before >= self.limit // 2:
                    self.limit = min(self.maximum, self.limit + 2 * self.quantum)
                if old != self.limit:
                    self.adjustments += 1
                self.last_adjust = now
        self._pump()

    def forget(self, flow):
        for ticket in list(self.waiters):
            if ticket[0] is flow:
                self.waiters.remove(ticket)
                if not ticket[2].done():
                    ticket[2].set_exception(ConnectionError("O fluxo foi encerrado."))
        self.release(flow, self.held.get(flow, 0))
        self._pump()

    def close(self):
        self.closed = True
        for ticket in self.waiters:
            if not ticket[2].done():
                ticket[2].set_exception(ConnectionError("O túnel foi encerrado."))
        self.waiters.clear()
        self.held.clear()
        self.grants.clear()
        self.borrowed = 0
        self.used = 0

    def diagnostics(self):
        return {"limit_bytes": self.limit, "admitted_bytes": self.used, "startup_borrowed_bytes": self.borrowed,
                "waiting_flows": len(self.waiters), "adjustments": self.adjustments,
                "base_ack_rtt_ms": round(self.base_rtt * 1000, 2) if self.base_rtt is not None else None,
                "latest_ack_rtt_ms": round(self.last_rtt * 1000, 2) if self.last_rtt is not None else None}
