"""Precise network deadlines on Python 3.12/Windows without changing OS timers."""
import asyncio
import time


class NetworkEventLoop(asyncio.SelectorEventLoop):
    def __init__(self):
        super().__init__()
        # BaseEventLoop otherwise uses GetTickCount64's 15.6 ms resolution on
        # this runtime and may dispatch retransmission/pacing timers early.
        self._clock_resolution = time.get_clock_info("perf_counter").resolution

    def time(self):
        return time.perf_counter()
