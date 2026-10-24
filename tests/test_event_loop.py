import asyncio
import time
import unittest

from pontelan.event_loop import NetworkEventLoop


class EventLoopTests(unittest.TestCase):
    def test_short_network_timers_do_not_fire_one_clock_tick_early(self):
        async def measure():
            for delay in (.005, .020, .035):
                started = time.perf_counter()
                await asyncio.sleep(delay)
                self.assertGreaterEqual(time.perf_counter() - started + .0005, delay)
        loop = NetworkEventLoop()
        try:
            self.assertEqual(loop._clock_resolution, time.get_clock_info('perf_counter').resolution)
            self.assertLess(abs(loop.time() - time.perf_counter()), .01)
            loop.run_until_complete(measure())
        finally:
            loop.close()
