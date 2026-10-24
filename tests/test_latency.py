import unittest
from types import SimpleNamespace

from pontelan.latency import LatencySamples, format_rtt, summary


class LatencyTests(unittest.TestCase):
    def test_precise_clock_and_no_zero_label(self):
        now = [1.0]
        samples = LatencySamples(clock=lambda: now[0])
        samples.probe('a')
        now[0] += .00042
        self.assertAlmostEqual(samples.pong('a'), .42)
        self.assertEqual(samples.latest(), .42)
        self.assertEqual(format_rtt(.42), '<1 ms')
        self.assertEqual(format_rtt(None), '—')

    def test_slow_replies_survive_next_probe_and_stale_expires(self):
        now = [1.0]
        samples = LatencySamples(clock=lambda: now[0])
        samples.probe('slow')
        now[0] = 4.0
        samples.probe('next')
        now[0] = 4.5
        self.assertEqual(samples.pong('slow'), 3500)
        now[0] = 15
        self.assertIsNone(samples.latest())
        diagnostics = samples.diagnostics()
        self.assertEqual(diagnostics['probe_timeouts'], 1)
        self.assertEqual(diagnostics['probes_received'], 1)

    def test_rolling_statistics_keep_real_peaks_and_bound_memory(self):
        now = [0.0]
        samples = LatencySamples(clock=lambda: now[0], limit=4)
        for index, value in enumerate([100, 300, 50, .2, 200]):
            samples.probe(str(index))
            now[0] += value / 1000
            samples.pong(str(index))
        data = samples.diagnostics()
        self.assertEqual(data['count'], 4)
        self.assertEqual(data['p95_ms'], 300)
        self.assertEqual(data['max_ms'], 300)
        self.assertEqual(data['latest_ms'], 200)
        self.assertIsNotNone(data['rtt_variation_ms'])
        self.assertNotIn('ip', data)

    def test_unknown_pong_and_bad_values(self):
        samples = LatencySamples()
        self.assertIsNone(samples.pong('unknown'))
        self.assertEqual(summary([float('inf'), float('nan')])['count'], 0)

    def test_diagnostics_do_not_present_expired_rtt_as_current(self):
        from pontelan.core import TunnelService
        now = [1.0]
        samples = LatencySamples(clock=lambda: now[0])
        samples.probe('a')
        now[0] += .05
        samples.pong('a')
        now[0] += 11
        service = TunnelService(lambda e: None)
        service.peers['test'] = SimpleNamespace(
            pc=SimpleNamespace(connectionState='connected'), route=lambda: 'host ↔ host',
            rtt_ms=50, latency=samples, streams=set())
        self.assertIsNone(service.diagnostics()['peers'][0]['rtt_ms'])


if __name__ == '__main__':
    unittest.main()
