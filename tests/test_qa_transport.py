"""Lightweight harness integrity/configuration checks; no sockets or benchmark."""
import hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pontelan import core
import qa_baseline_022 as baseline_022
from qa_transport import (build_parser, install_config, install_release_trace,
                          original_read, probe_series, sctp_diagnostics, ShapedUDP,
                          series_extrema, source_fingerprint)


class BenchmarkConfigurationTests(unittest.TestCase):
    def configuration(self):
        return (core.StreamBridge._read,
                {name: getattr(core, name) for name in
                 ("DeliveryWindow", "CHUNK", "SEND_CHUNK", "PEER_SEND_BUDGET", "WINDOW")})

    def test_default_preserves_legacy_baseline_and_exact_candidate_defaults(self):
        args = build_parser().parse_args([])
        self.assertEqual(args.baseline, "legacy")
        self.assertIsNone(args.candidate_window)
        self.assertEqual((args.loss_percent, args.jitter_ms, args.seed), (0, 0, 20261024))
        before = self.configuration()
        restore = install_config("candidate")
        try:
            self.assertEqual(self.configuration(), before)
        finally:
            restore()
        self.assertEqual(self.configuration(), before)

    def test_published_baseline_patches_sender_class_constants_and_constructor(self):
        before = self.configuration()
        restore = install_config("baseline", candidate_window=17, baseline="0.2.2")
        try:
            self.assertIs(core.StreamBridge._read, baseline_022._read)
            self.assertIs(core.DeliveryWindow, baseline_022.DeliveryWindow)
            self.assertEqual((core.CHUNK, core.SEND_CHUNK, core.PEER_SEND_BUDGET, core.WINDOW),
                             (16384, 4096, 131072, 262144))
            with patch.object(core, "RTCPeerConnection", return_value=Mock()):
                peer = core.TunnelPeer(SimpleNamespace(stun=False), "qa", "QA")
            self.assertIs(type(peer.delivery), baseline_022.DeliveryWindow)
            self.assertEqual(peer.delivery.limit, 131072)
            self.assertEqual(peer.delivery.quantum, 4096)
        finally:
            restore()
        after = self.configuration()
        self.assertIs(after[0], before[0])
        for name, value in before[1].items():
            self.assertIs(after[1][name], value)

    def test_legacy_default_and_explicit_candidate_override_restore_exactly(self):
        before = self.configuration()
        restore = install_config("baseline", candidate_window=17)
        try:
            self.assertIs(core.StreamBridge._read, original_read)
            self.assertEqual(core.PEER_SEND_BUDGET, 1 << 30)
            self.assertIs(core.DeliveryWindow, before[1]["DeliveryWindow"])
        finally:
            restore()
        restore = install_config("candidate", candidate_window=32768, baseline="0.2.2")
        try:
            self.assertIs(core.StreamBridge._read, before[0])
            self.assertIs(core.DeliveryWindow, before[1]["DeliveryWindow"])
            self.assertEqual(core.PEER_SEND_BUDGET, 32768)
        finally:
            restore()
        self.assertEqual(self.configuration(), before)

    def test_nested_configs_restore_the_callers_original_objects(self):
        before = self.configuration()
        outer = install_config("candidate", 49152)
        middle = self.configuration()
        inner = install_config("baseline", baseline="0.2.2")
        inner()
        self.assertEqual(self.configuration(), middle)
        outer()
        self.assertEqual(self.configuration(), before)

    def test_frozen_component_fingerprints_match_manifest(self):
        self.assertEqual(baseline_022.SOURCE["revision"], "9ec2f7d2f9392aceb8d25f5e1d81d154bcd12e6b")
        self.assertEqual(source_fingerprint(baseline_022._read), baseline_022.SOURCE["stream_read_sha256"])
        self.assertEqual(source_fingerprint(baseline_022.DeliveryWindow), baseline_022.SOURCE["delivery_window_sha256"])
        source = Path(baseline_022.__file__).read_text(encoding="utf-8")
        flow = source[source.index('"""Fair, delay-sensitive'):source.index("\n\nBASELINE_VERSION")]
        self.assertEqual(hashlib.sha256(flow.encode()).hexdigest(), baseline_022.SOURCE["flow_sha256"])

    def test_optional_private_counters_report_lengths_and_never_payloads(self):
        secret = b"payload-must-not-appear"
        sctp = SimpleNamespace(_cwnd=3600, _flight_size=1200,
                               _outbound_queue=[SimpleNamespace(user_data=secret)],
                               _data_channel_queue=[(object(), 1, secret)],
                               _sent_queue=[], _srtt=.04, _rto=1)
        values = sctp_diagnostics(SimpleNamespace(pc=SimpleNamespace(sctp=sctp), streams=[]))
        self.assertEqual(values["cwnd_bytes"], 3600)
        self.assertEqual(values["outbound_queue_payload_bytes"], len(secret))
        self.assertEqual(values["data_channel_queue_payload_bytes"], len(secret))
        self.assertEqual(values["srtt_ms"], 40)
        self.assertNotIn(secret.decode(), str(values))
        self.assertEqual(sctp_diagnostics(SimpleNamespace()), {})
        self.assertIsNone(sctp_diagnostics(SimpleNamespace(pc=SimpleNamespace(sctp=object())))["cwnd_bytes"])

    def test_series_extrema_ignore_optional_unavailable_counters(self):
        self.assertEqual(series_extrema([{"cwnd": 12, "srtt": None}, {"cwnd": 4, "srtt": 30}]),
                         {"cwnd": {"minimum": 4, "maximum": 12}, "srtt": {"minimum": 30, "maximum": 30}})

    def test_release_trace_records_only_changes_and_restores_exact_method(self):
        class Window:
            def __init__(self):
                self.limit, self.used, self.waiters, self.startup = 100, 80, [object()], True

            def release(self, flow, count, rtt=None):
                self.used -= count
                if rtt is not None:
                    self.limit //= 2
                    self.startup = False
                return "unchanged return value"

        original, records = Window.release, []
        restore = install_release_trace(Window, records, arm_started=8, clock=lambda: 10)
        window = Window()
        flow = SimpleNamespace(peer=SimpleNamespace(owner=SimpleNamespace(role="guest")))
        try:
            self.assertEqual(window.release(flow, 3), "unchanged return value")
            self.assertEqual(records, [])
            window.release(flow, 5, .4)
            window.release(flow, 7, .2)
            self.assertEqual(records[0], {"arm_seconds": 2, "side": "guest", "window": "window-1",
                                           "count_bytes": 5, "rtt_seconds": .4,
                                           "sample_sent_arm_seconds": 1.6, "before_used_bytes": 77,
                                           "loaded": True, "old_limit_bytes": 100, "new_limit_bytes": 50,
                                           "startup_before": True, "startup_after": False})
            self.assertEqual(records[1]["window"], "window-1")
            self.assertFalse(records[1]["startup_before"])
        finally:
            restore()
        self.assertIs(Window.release, original)

    def test_baseline_trace_leaves_production_method_untouched_and_restores_on_error(self):
        production_release = core.DeliveryWindow.release
        frozen_release = baseline_022.DeliveryWindow.release
        restore_config = install_config("baseline", baseline="0.2.2")
        restore_trace = install_release_trace(core.DeliveryWindow, [], arm_started=0)
        try:
            self.assertIsNot(core.DeliveryWindow.release, frozen_release)
            self.assertIsNot(core.DeliveryWindow.release, production_release)
            with self.assertRaises(AttributeError):
                core.DeliveryWindow.release(object(), object(), 1)
        finally:
            restore_trace()
            restore_config()
        self.assertIs(baseline_022.DeliveryWindow.release, frozen_release)
        self.assertIs(core.DeliveryWindow.release, production_release)

    def test_probe_series_exports_send_time_and_keeps_missing_replies(self):
        probe = SimpleNamespace(started=[10.5, 11.0, 12.1], samples=[(11.0, 901.123456), (10.5, 50)])
        self.assertEqual(probe_series(probe, 10, 11, 12), [
            {"probe": 1, "sent_arm_seconds": .5, "sent_loaded_seconds": -.5,
             "phase": "idle", "rtt_ms": 50},
            {"probe": 2, "sent_arm_seconds": 1, "sent_loaded_seconds": 0,
             "phase": "loaded", "rtt_ms": 901.123},
            {"probe": 3, "sent_arm_seconds": 2.1, "sent_loaded_seconds": 1.1,
             "phase": "after_load", "rtt_ms": None}])


class UDPShaperTests(unittest.TestCase):
    def shaped(self, **kwargs):
        transport = Mock()
        transport.is_closing.return_value = False
        protocol = SimpleNamespace(transport=transport)
        shaper = ShapedUDP(protocol, rate=1000, delay=.02, **kwargs)
        self.addCleanup(shaper.restore)
        return shaper, protocol, transport

    def loop(self):
        loop = Mock()
        loop.time.return_value = 0
        # Each timer needs its own identity, as in a real asyncio event loop.
        loop.call_at.side_effect = lambda *args: Mock()
        return loop

    def test_all_lost_datagrams_still_consume_serialization_capacity(self):
        shaper, _, transport = self.shaped(loss_percent=100)
        loop = self.loop()
        with patch("qa_transport.asyncio.get_running_loop", return_value=loop):
            shaper.sendto(bytes(100))
            shaper.sendto(bytes(200))
        self.assertAlmostEqual(shaper.next_send, .3)
        self.assertEqual(shaper.dropped_datagrams, 2)
        self.assertEqual(shaper.dropped_bytes, 300)
        self.assertEqual(shaper.datagrams, 2)
        self.assertEqual(shaper.bytes, 300)
        self.assertEqual(loop.call_at.call_count, 0)
        transport.sendto.assert_not_called()

    def test_same_seed_reproduces_loss_and_delay_schedule(self):
        schedules = []
        for seed in (17, 17, 18):
            shaper, _, _ = self.shaped(loss_percent=30, jitter_ms=5, seed=seed)
            loop = self.loop()
            with patch("qa_transport.asyncio.get_running_loop", return_value=loop):
                for _ in range(50):
                    shaper.sendto(bytes(10))
            scheduled = [call.args[0] for call in loop.call_at.call_args_list]
            self.assertEqual(len(scheduled) + shaper.dropped_datagrams, 50)
            # Serialization completes in multiples of 10 ms; fixed propagation
            # plus uniform jitter must keep each arrival inside its 5 ms slot.
            for arrival in scheduled:
                extra = (arrival - .02) % .01
                self.assertLessEqual(extra, .005000001)
            schedules.append((scheduled, shaper.dropped_datagrams))
        self.assertEqual(schedules[0], schedules[1])
        self.assertNotEqual(schedules[0], schedules[2])

    def test_default_has_exact_fixed_delay_and_restore_cancels_timers(self):
        shaper, protocol, transport = self.shaped()
        loop = self.loop()
        with patch("qa_transport.asyncio.get_running_loop", return_value=loop):
            shaper.sendto(bytes(100))
        self.assertAlmostEqual(loop.call_at.call_args.args[0], .12)
        self.assertEqual(shaper.dropped_datagrams, 0)
        self.assertIs(protocol.transport, shaper)
        handles = tuple(shaper.handles)
        shaper.restore()
        self.assertIs(protocol.transport, transport)
        for handle in handles:
            handle.cancel.assert_called_once()
        loop.call_at.call_args.args[1]()
        transport.sendto.assert_not_called()

    def test_invalid_impairment_settings_are_rejected(self):
        for settings in ({"loss_percent": -1}, {"loss_percent": 101},
                         {"jitter_ms": -1}, {"jitter_ms": float("inf")}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.shaped(**settings)


if __name__ == "__main__":
    unittest.main()
