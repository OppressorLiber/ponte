"""Admission scheduling, cancellation and adaptive byte-accounting checks."""
import asyncio
import unittest

from pontelan.flow import DeliveryWindow


Q = 4096


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds=.2):
        self.now += seconds


class DeliveryWindowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tasks = []
        self.windows = []

    async def asyncTearDown(self):
        for window in self.windows:
            window.close()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def window(self, initial=4 * Q, **kwargs):
        window = DeliveryWindow(initial, minimum=4 * Q, quantum=Q, **kwargs)
        self.windows.append(window)
        return window

    def queued(self, window, flow, count=Q):
        task = asyncio.create_task(window.acquire(flow, count))
        self.tasks.append(task)
        return task

    def accounting(self, window):
        self.assertEqual(window.used, sum(window.held.values()))
        self.assertGreaterEqual(window.used, 0)
        self.assertTrue(all(count > 0 for count in window.held.values()))
        self.assertEqual(window.borrowed,
                         sum(count for grants in window.grants.values()
                             for count, borrowed in grants if borrowed))
        self.assertEqual(window.used,
                         sum(count for grants in window.grants.values() for count, _ in grants))

    async def saturated(self):
        window = self.window()
        bulk, reserve = object(), object()
        await window.acquire(bulk, 4 * Q)
        await window.acquire(reserve, Q)
        self.assertEqual(window.used, window.limit + Q)
        return window, bulk, reserve

    async def test_equal_chunks_obtain_available_capacity_in_arrival_order(self):
        window, _, reserve = await self.saturated()
        first, second = object(), object()
        a = self.queued(window, first)
        b = self.queued(window, second)
        await asyncio.sleep(0)
        self.assertFalse(a.done())
        self.assertFalse(b.done())
        window.release(reserve, Q)
        await asyncio.sleep(0)
        self.assertTrue(a.done())
        self.assertFalse(b.done())
        window.release(first, Q)
        await asyncio.wait_for(b, 1)
        self.accounting(window)

    async def test_smaller_request_uses_space_behind_a_blocked_large_request(self):
        window, _, reserve = await self.saturated()
        large_flow, small_flow = object(), object()
        large = self.queued(window, large_flow, 2 * Q)
        small = self.queued(window, small_flow, Q)
        await asyncio.sleep(0)
        window.release(reserve, Q)
        await asyncio.wait_for(small, 1)
        self.assertFalse(large.done())
        self.assertNotIn(large_flow, window.held)
        self.accounting(window)

    async def test_startup_reserve_is_bounded_across_many_new_flows(self):
        window = self.window(16 * Q)
        bulk = object()
        await window.acquire(bulk, 16 * Q)
        flows = [object() for _ in range(8)]
        tasks = [self.queued(window, flow) for flow in flows]
        await asyncio.sleep(0)
        self.assertEqual(sum(task.done() for task in tasks), 4)
        self.assertEqual(window.used, window.limit + 4 * Q)
        self.assertEqual(len(window.waiters), 4)
        self.accounting(window)

    async def test_one_stalled_flow_allows_repeated_unrelated_small_requests(self):
        window = self.window()
        stalled = object()
        await window.acquire(stalled, window.limit)
        for count in (1, 512, Q, 37, Q, 1024):
            interactive = object()
            await asyncio.wait_for(window.acquire(interactive, count), 1)
            self.assertEqual(window.held[stalled], window.limit)
            window.release(interactive, count, .04)
            self.assertEqual(window.used, window.limit)
            self.accounting(window)

    async def test_multiple_flow_releases_return_exact_bytes(self):
        window = self.window()
        first, second = object(), object()
        await window.acquire(first, 3000)
        await window.acquire(second, 7000)
        await window.acquire(first, 17)
        window.release(first, 2999)
        self.assertEqual(window.held[first], 18)
        self.assertEqual(window.used, 7018)
        window.release(second, 2000)
        self.assertEqual(window.used, 5018)
        window.forget(first)
        self.assertEqual(window.used, 5000)
        window.release(second, 5000)
        self.assertEqual(window.used, 0)
        self.accounting(window)

    async def test_cancel_queued_waiter_does_not_consume_bytes(self):
        window, _, _ = await self.saturated()
        task = self.queued(window, object())
        await asyncio.sleep(0)
        before = window.used
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(window.used, before)
        self.assertFalse(window.waiters)
        self.accounting(window)

    async def test_cancel_after_grant_reclaims_only_that_grant(self):
        window, _, reserve = await self.saturated()
        flow = object()
        task = self.queued(window, flow)
        await asyncio.sleep(0)
        window.release(reserve, Q)
        self.assertEqual(window.held[flow], Q)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn(flow, window.held)
        self.assertEqual(window.used, 4 * Q)
        self.accounting(window)

    async def test_forget_wakes_waiter_and_returns_its_existing_reservations(self):
        window, bulk, _ = await self.saturated()
        task = self.queued(window, bulk)
        await asyncio.sleep(0)
        window.forget(bulk)
        with self.assertRaises(ConnectionError):
            await task
        self.assertNotIn(bulk, window.held)
        self.assertEqual(window.used, Q)
        self.assertFalse(window.waiters)
        self.accounting(window)

    async def test_forget_after_grant_then_cancel_cannot_release_other_flows(self):
        window, _, reserve = await self.saturated()
        flow = object()
        task = self.queued(window, flow)
        await asyncio.sleep(0)
        window.release(reserve, Q)
        self.assertEqual(window.held[flow], Q)
        window.forget(flow)
        remaining = window.used
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(window.used, remaining)
        self.accounting(window)

    async def test_close_rejects_waiters_and_future_acquisitions(self):
        window, _, _ = await self.saturated()
        task = self.queued(window, object())
        await asyncio.sleep(0)
        window.close()
        with self.assertRaises(ConnectionError):
            await task
        with self.assertRaises(ConnectionError):
            await window.acquire(object(), 1)
        self.assertFalse(window.waiters)
        self.assertEqual(window.used, 0)
        self.accounting(window)

    async def test_unloaded_or_invalid_rtt_samples_do_not_grow_window(self):
        clock = FakeClock()
        window = self.window(clock=clock)
        flow = object()
        for sample in (None, 0, -1, float('nan'), float('inf'), .05):
            await window.acquire(flow, Q)
            window.release(flow, Q, sample)
            clock.advance()
        self.assertEqual(window.limit, 4 * Q)
        self.assertEqual(window.adjustments, 0)
        self.assertEqual(window.base_rtt, .05)
        self.accounting(window)

    async def test_backlogged_fast_delivery_grows_to_maximum_and_respects_epoch(self):
        clock = FakeClock()
        window = self.window(8 * Q, maximum=12 * Q, clock=clock)
        bulk = object()
        await window.acquire(bulk, window.limit)
        # A deliberately oversized queued acquisition keeps demand observable;
        # it cannot consume either the normal budget or startup reserve.
        waiting = self.queued(window, object(), 20 * Q)
        await asyncio.sleep(0)
        window.release(bulk, Q, .05)
        self.assertEqual(window.limit, 10 * Q)
        first = window.adjustments
        window.release(bulk, Q, .05)
        self.assertEqual(window.adjustments, first)
        await window.acquire(bulk, 4 * Q)
        clock.advance()
        window.release(bulk, Q, .05)
        self.assertEqual(window.limit, 12 * Q)
        clock.advance()
        window.release(bulk, Q, .05)
        self.assertEqual(window.limit, window.maximum)
        self.assertEqual(window.adjustments, 2)
        self.assertFalse(waiting.done())
        self.accounting(window)

    async def test_delay_reduces_budget_to_minimum_without_losing_admitted_bytes(self):
        clock = FakeClock()
        window = self.window(16 * Q, clock=clock)
        flow = object()
        await window.acquire(flow, window.limit)
        window.release(flow, 1, .01)
        for _ in range(20):
            clock.advance()
            window.release(flow, 1, 1.0)
            self.assertGreaterEqual(window.limit, window.minimum)
            self.accounting(window)
        self.assertEqual(window.limit, window.minimum)
        self.assertGreater(window.used, window.limit)
        self.assertGreater(window.adjustments, 0)
        window.release(flow, window.held[flow])
        self.assertEqual(window.used, 0)

    async def test_shrink_below_admitted_bytes_keeps_independent_reserve_bounded_and_reusable(self):
        clock = FakeClock()
        window = self.window(16 * Q, clock=clock)
        bulk = object()
        await window.acquire(bulk, window.limit)
        window.release(bulk, 1, .01)
        clock.advance()
        window.release(bulk, 1, 1.0)
        self.assertEqual(window.limit, 12 * Q)
        reserve = window.limit // 4
        bulk_held = window.held[bulk]
        # Reproduce the real stalled-writer failure: even the total old
        # allowance exceeds the shrunken limit plus its startup reserve.
        self.assertGreater(window.used, window.limit + reserve)
        first, second, third, fourth, tiny = (object() for _ in range(5))
        for flow in (first, second, third):
            await asyncio.wait_for(window.acquire(flow, Q), 1)
        self.assertEqual(window.borrowed, reserve)
        self.assertEqual(window.used, bulk_held + reserve)
        waiting = self.queued(window, fourth)
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        window.release(first, Q // 2)
        self.assertEqual(window.borrowed, reserve - Q // 2)
        self.assertFalse(waiting.done())
        # Half a returned startup chunk admits half a new chunk, but cannot
        # over-admit the earlier full-size waiter.
        await asyncio.wait_for(window.acquire(tiny, Q // 2), 1)
        self.assertEqual(window.borrowed, reserve)
        self.assertFalse(waiting.done())
        window.release(first, Q // 2)
        window.release(tiny, Q // 2)
        await asyncio.wait_for(waiting, 1)
        self.assertEqual(window.borrowed, reserve)
        self.assertEqual(window.used, bulk_held + reserve)
        self.accounting(window)
        for flow in (second, third, fourth):
            window.forget(flow)
            self.accounting(window)
        self.assertEqual(window.borrowed, 0)
        self.assertEqual(window.used, bulk_held)
        window.forget(bulk)
        self.assertEqual(window.used, 0)
        self.assertFalse(window.grants)
        self.accounting(window)

    async def test_partial_fifo_release_consumes_startup_grant_before_later_normal_grant(self):
        window = self.window()
        bulk, flow = object(), object()
        await window.acquire(bulk, window.limit)
        await window.acquire(flow, Q)
        self.assertEqual(window.borrowed, Q)
        window.release(bulk, 2 * Q)
        await window.acquire(flow, Q)
        self.assertEqual(list(window.grants[flow]), [[Q, True], [Q, False]])
        window.release(flow, Q // 2)
        self.assertEqual(window.borrowed, Q // 2)
        self.assertEqual(list(window.grants[flow]), [[Q // 2, True], [Q, False]])
        window.release(flow, Q)
        self.assertEqual(window.borrowed, 0)
        self.assertEqual(list(window.grants[flow]), [[Q // 2, False]])
        self.assertEqual(window.held[flow], Q // 2)
        self.accounting(window)
        window.forget(flow)
        self.assertNotIn(flow, window.grants)
        self.assertEqual(window.used, window.held[bulk])
        window.close()
        self.assertEqual(window.borrowed, 0)
        self.assertFalse(window.grants)
        self.accounting(window)

    async def test_cancel_later_normal_grant_preserves_older_unacknowledged_startup_grant(self):
        window = self.window()
        bulk, flow = object(), object()
        await window.acquire(bulk, window.limit)
        await window.acquire(flow, Q)
        older = window.grants[flow][0]
        pending = self.queued(window, flow)
        await asyncio.sleep(0)
        self.assertFalse(pending.done())
        window.release(bulk, 2 * Q)
        self.assertEqual(list(window.grants[flow]), [[Q, True], [Q, False]])
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(window.borrowed, Q)
        self.assertEqual(window.held[flow], Q)
        self.assertEqual(len(window.grants[flow]), 1)
        self.assertIs(window.grants[flow][0], older)
        self.assertEqual(older, [Q, True])
        self.accounting(window)
        window.release(flow, Q)
        self.assertEqual(window.borrowed, 0)
        self.assertEqual(window.used, window.held[bulk])
        self.accounting(window)

    async def test_cancel_exact_grant_after_forget_or_close_cannot_double_refund(self):
        for action in ('forget', 'close'):
            with self.subTest(action=action):
                window = self.window()
                bulk, flow = object(), object()
                await window.acquire(bulk, window.limit)
                await window.acquire(flow, Q)
                pending = self.queued(window, flow)
                await asyncio.sleep(0)
                window.release(bulk, 2 * Q)
                self.assertEqual(list(window.grants[flow]), [[Q, True], [Q, False]])
                if action == 'forget':
                    window.forget(flow)
                else:
                    window.close()
                before = window.used
                held_before = dict(window.held)
                pending.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await pending
                self.assertEqual(window.used, before)
                self.assertEqual(window.held, held_before)
                self.assertEqual(window.borrowed, 0)
                self.assertNotIn(flow, window.grants)
                self.accounting(window)


if __name__ == '__main__':
    unittest.main()
