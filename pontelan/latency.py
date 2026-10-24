"""Bounded app-level RTT samples, freshness, and loop delay. No addresses."""
from collections import deque
import math
import statistics
import time


def summary(values):
    values = sorted(v for v in values if isinstance(v, (int, float)) and math.isfinite(v))
    if not values:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
    return {"count": len(values), "p50_ms": round(statistics.median(values), 2),
            "p95_ms": round(values[max(0, math.ceil(.95 * len(values)) - 1)], 2),
            "max_ms": round(values[-1], 2)}


def format_rtt(value):
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return "—"
    return "<1 ms" if value < 1 else f"{round(value)} ms"


class LatencySamples:
    def __init__(self, clock=time.perf_counter, limit=120, timeout=10):
        self.clock, self.timeout = clock, timeout
        self.samples = deque(maxlen=limit)
        self.pending = {}
        self.sent = self.received = self.timed_out = 0

    def expire(self):
        now = self.clock()
        for identifier, start in list(self.pending.items()):
            if now - start >= self.timeout:
                self.pending.pop(identifier)
                self.timed_out += 1

    def probe(self, identifier):
        self.expire()
        self.pending[identifier] = self.clock()
        self.sent += 1

    def pong(self, identifier):
        self.expire()
        start = self.pending.pop(identifier, None)
        if start is None:
            return None
        now = self.clock()
        value = max(0, (now - start) * 1000)
        self.samples.append((now, value))
        self.received += 1
        return value

    def latest(self):
        if not self.samples or self.clock() - self.samples[-1][0] >= self.timeout:
            return None
        return round(self.samples[-1][1], 2)

    def diagnostics(self):
        self.expire()
        values = [value for _, value in self.samples]
        result = summary(values)
        result.update({"latest_ms": self.latest(),
                       "sample_age_seconds": round(self.clock() - self.samples[-1][0], 2) if self.samples else None,
                       "rtt_variation_ms": round(statistics.mean(abs(a-b) for a, b in zip(values, values[1:])), 2)
                           if len(values) > 1 else None,
                       "probes_sent": self.sent, "probes_received": self.received,
                       "probe_timeouts": self.timed_out,
                       "samples_ms": [round(v, 2) for v in values],
                       "meaning": "RTT do aplicativo; timeouts não equivalem à perda de pacotes da rede"})
        return result
