"""Controlled same-PC TCP/ICE/DTLS/SCTP A/B; never attaches to a user's room.

Signaling uses its real authenticated envelope, delivered in memory instead of
HTTP. A FIFO rate/delay shaper wraps actual ICE UDP transports after DTLS is up.
All latency probes are 64-byte TCP request/response traffic through the tunnel.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import hashlib
import inspect
import json
import logging
import math
import os
from pathlib import Path
import random
import struct
import threading
import time
import textwrap

from pontelan import core
from pontelan.latency import summary
import qa_baseline_022 as baseline_022


REAL_BUS = core.SignalBus


class MemorySignalBus(REAL_BUS):
    registry = {}

    async def start(self):
        self.registry[self.code, self.peer_id] = self
        self._ready.set()

    async def send(self, target, message):
        destination = self.registry.get((self.code, target))
        if self._closed or destination is None or destination._closed:
            raise ConnectionError("Benchmark signaling recipient unavailable")
        decoded = destination._decode(self._encode(target, message))
        if decoded is None:
            raise AssertionError("Authenticated signaling did not round-trip")
        await destination.on_message(decoded)

    async def close(self):
        self._closed = True
        self.registry.pop((self.code, self.peer_id), None)


class ShapedUDP:
    """FIFO serialization, seeded loss and independent extra propagation delay."""

    def __init__(self, protocol, rate, delay, *, loss_percent=0, jitter_ms=0, seed=20261024):
        if not 0 <= loss_percent <= 100 or not math.isfinite(jitter_ms) or jitter_ms < 0:
            raise ValueError("Loss must be 0..100 percent and jitter must be finite and nonnegative")
        self.protocol = protocol
        self.transport = protocol.transport
        self.rate, self.delay = rate, delay
        self.loss_probability, self.jitter = loss_percent / 100, jitter_ms / 1000
        self.seed, self.random = seed, random.Random(seed)
        self.next_send = 0.0
        self.handles = set()
        self.closed = False
        self.bytes = self.datagrams = self.send_errors = 0
        self.dropped_datagrams = self.dropped_bytes = 0
        self.maximum_queue_ms = 0.0
        protocol.transport = self

    def sendto(self, data, addr=None):
        if self.closed:
            return
        loop = asyncio.get_running_loop()
        now = loop.time()
        self.next_send = max(self.next_send, now) + len(data) / self.rate
        self.maximum_queue_ms = max(self.maximum_queue_ms, (self.next_send - now) * 1000)
        self.bytes += len(data)
        self.datagrams += 1
        dropped = self.random.random() < self.loss_probability if self.loss_probability else False
        jitter = self.random.uniform(0, self.jitter) if self.jitter else 0
        # The lost datagram still occupied its complete link serialization slot.
        if dropped:
            self.dropped_datagrams += 1
            self.dropped_bytes += len(data)
            return
        holder = []

        def deliver():
            self.handles.discard(holder[0])
            if self.closed or self.transport.is_closing():
                return
            try:
                self.transport.sendto(data, addr)
            except (OSError, RuntimeError):
                self.send_errors += 1

        handle = loop.call_at(self.next_send + self.delay + jitter, deliver)
        holder.append(handle)
        self.handles.add(handle)

    def __getattr__(self, name):
        return getattr(self.transport, name)

    def restore(self):
        self.closed = True
        for handle in self.handles:
            handle.cancel()
        self.handles.clear()
        if self.protocol.transport is self:
            self.protocol.transport = self.transport


async def original_read(self):
    """Frozen original 256 KiB per-stream credit behavior, before send budgets."""
    while not self.closed:
        data = await self.reader.read(core.CHUNK)
        if not data:
            self.sent_eof = True
            self.channel.send(b"\x01")
            await asyncio.wait_for(self.eof_acked.wait(), 60)
            return
        while self.credit < len(data) or len(self.pending_sizes) >= 128:
            self.credit_changed.clear()
            if self.closed:
                return
            await self.credit_changed.wait()
        self.credit -= len(data)
        self.pending_sizes.append(len(data))
        if self.closed or self.channel.readyState != "open":
            return
        self.channel.send(b"\x00" + data)
        self.peer.owner.sent += len(data)


def install_config(mode, candidate_window=None, baseline="legacy"):
    """Install only this arm's transport configuration; restore exact objects.

    The legacy comparison remains the default. 0.2.2 freezes the published
    sender, admission class and constants. Candidate uses unmodified production
    unless an explicit exploratory initial-budget override was requested.
    """
    if mode not in ("baseline", "candidate") or baseline not in ("legacy", "0.2.2"):
        raise ValueError("Unknown benchmark arm or baseline")
    original = core.StreamBridge._read
    absent = object()
    changes = {}

    def replace(name, value):
        changes[name] = getattr(core, name, absent)
        setattr(core, name, value)

    if mode == "baseline" and baseline == "0.2.2":
        core.StreamBridge._read = baseline_022._read
        replace("DeliveryWindow", baseline_022.DeliveryWindow)
        for name in ("CHUNK", "SEND_CHUNK", "PEER_SEND_BUDGET", "WINDOW"):
            replace(name, getattr(baseline_022, name))
    elif mode == "baseline":
        core.StreamBridge._read = original_read
        if hasattr(core, "PEER_SEND_BUDGET"):
            replace("PEER_SEND_BUDGET", 1 << 30)
    elif candidate_window is not None:
        if not hasattr(core, "PEER_SEND_BUDGET"):
            raise ValueError("Production has no initial send budget to override")
        replace("PEER_SEND_BUDGET", candidate_window)

    def restore():
        core.StreamBridge._read = original
        for name, value in changes.items():
            if value is absent:
                delattr(core, name)
            else:
                setattr(core, name, value)

    return restore


def source_fingerprint(value):
    return hashlib.sha256(textwrap.dedent(inspect.getsource(value)).rstrip().encode()).hexdigest()


def install_release_trace(window_class, records, arm_started, clock=time.perf_counter):
    """Observe actual limit transitions without changing admission decisions."""
    original = window_class.release
    aliases = {}

    @functools.wraps(original)
    def traced(window, flow, count, rtt=None):
        old_limit, before_used = window.limit, window.used
        loaded, startup_before = bool(window.waiters), getattr(window, "startup", None)
        result = original(window, flow, count, rtt)
        if window.limit != old_limit:
            elapsed = clock() - arm_started
            side = getattr(getattr(getattr(flow, "peer", None), "owner", None), "role", "unknown")
            if side not in ("host", "guest"):
                side = "unknown"
            alias = aliases.setdefault(window, f"window-{len(aliases) + 1}")
            records.append({"arm_seconds": round(elapsed, 6), "side": side, "window": alias,
                            "count_bytes": count, "rtt_seconds": rtt,
                            "sample_sent_arm_seconds": round(elapsed - rtt, 6) if rtt is not None else None,
                            "before_used_bytes": before_used, "loaded": loaded,
                            "old_limit_bytes": old_limit, "new_limit_bytes": window.limit,
                            "startup_before": startup_before,
                            "startup_after": getattr(window, "startup", None)})
        return result

    window_class.release = traced

    def restore():
        window_class.release = original

    return restore


def probe_series(probe, arm_started, loaded_start, loaded_end):
    """Every sent request, sorted by send time; missing RTTs remain explicit."""
    replies = dict(probe.samples)
    return [{"probe": number, "sent_arm_seconds": round(start - arm_started, 6),
             "sent_loaded_seconds": round(start - loaded_start, 6),
             "phase": "idle" if start < loaded_start else "loaded" if start <= loaded_end else "after_load",
             "rtt_ms": round(replies[start], 3) if start in replies else None}
            for number, start in sorted(enumerate(probe.started, 1), key=lambda pair: pair[1])]


def sctp_diagnostics(peer):
    """Private aiortc counters are QA-only, optional, and contain no payload/IP."""
    sctp = getattr(getattr(peer, "pc", None), "sctp", None)
    if sctp is None:
        return {}
    outbound = getattr(sctp, "_outbound_queue", None)
    channels = getattr(sctp, "_data_channel_queue", None)
    sent = getattr(sctp, "_sent_queue", None)

    def chunk_bytes(queue):
        return sum(len(getattr(chunk, "user_data", b"")) for chunk in queue) if queue is not None else None

    streams = tuple(getattr(peer, "streams", ()))
    srtt = getattr(sctp, "_srtt", None)
    rto = getattr(sctp, "_rto", None)
    return {"cwnd_bytes": getattr(sctp, "_cwnd", None),
            "flight_bytes": getattr(sctp, "_flight_size", None),
            "outbound_queue_chunks": len(outbound) if outbound is not None else None,
            "outbound_queue_payload_bytes": chunk_bytes(outbound),
            "data_channel_queue_messages": len(channels) if channels is not None else None,
            "data_channel_queue_payload_bytes": sum(len(item[2]) for item in channels) if channels is not None else None,
            "sent_queue_chunks": len(sent) if sent is not None else None,
            "sent_queue_payload_bytes": chunk_bytes(sent),
            "srtt_ms": round(srtt * 1000, 3) if srtt is not None else None,
            "rto_ms": round(rto * 1000, 3) if rto is not None else None,
            "stream_count": len(streams),
            "channel_buffered_bytes": sum(stream.channel.bufferedAmount for stream in streams),
            "pending_ack_bytes": sum(sum(stream.pending_sizes) for stream in streams),
            "tcp_reader_buffer_bytes": sum(len(getattr(stream.reader, "_buffer", ())) for stream in streams),
            "received_queued_bytes": sum(stream.queued_bytes for stream in streams)}


def series_extrema(samples):
    keys = {key for sample in samples for key, value in sample.items()
            if isinstance(value, (int, float))}
    return {key: {"minimum": min(sample[key] for sample in samples if sample.get(key) is not None),
                  "maximum": max(sample[key] for sample in samples if sample.get(key) is not None)}
            for key in sorted(keys)}


async def close_writer(writer):
    writer.close()
    with contextlib.suppress(OSError, TimeoutError):
        await asyncio.wait_for(writer.wait_closed(), .75)


async def wait_for_listener(service, events):
    async with asyncio.timeout(15):
        while not service.local_port:
            error = next((e for e in events if e.get("type") == "error"), None)
            if error:
                raise ConnectionError(error.get("message"))
            await asyncio.sleep(.01)


class TCPProbes:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.pending = {}
        self.samples = []
        self.started = []
        self.sent = self.received = self.invalid = self.timeouts = 0
        self.stop = asyncio.Event()
        self.consumer = asyncio.create_task(self.receive())
        self.producer = None

    async def receive(self):
        try:
            while True:
                reply = await self.reader.readexactly(64)
                identifier = struct.unpack("!Q", reply[:8])[0]
                record = self.pending.pop(identifier, None)
                if record is None or record[1] != reply:
                    self.invalid += 1
                    continue
                self.received += 1
                self.samples.append((record[0], (time.perf_counter() - record[0]) * 1000))
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            return

    async def produce(self, interval):
        while not self.stop.is_set():
            self.sent += 1
            packet = struct.pack("!Q", self.sent) + bytes(range(56))
            started = time.perf_counter()
            self.started.append(started)
            self.pending[self.sent] = (started, packet)
            self.writer.write(packet)
            await self.writer.drain()
            try:
                await asyncio.wait_for(self.stop.wait(), interval)
            except TimeoutError:
                pass

    async def finish(self, drain=3.0):
        self.stop.set()
        if self.producer:
            done, pending = await asyncio.wait((self.producer,), timeout=1)
            if pending:
                self.producer.cancel()
            await asyncio.gather(self.producer, return_exceptions=True)
        started = time.perf_counter()
        while self.pending and time.perf_counter() - started < drain:
            await asyncio.sleep(.01)
        self.timeouts = len(self.pending)
        self.consumer.cancel()
        await asyncio.gather(self.consumer, return_exceptions=True)
        return round(time.perf_counter() - started, 3)


async def run_arm(mode, mbps, rtt_ms, candidate_window, repetition, max_seconds, bulk_bytes=0,
                  baseline="legacy", loss_percent=0, jitter_ms=0, seed=20261024, checkpoint=None):
    restore_config = install_config(mode, candidate_window, baseline)
    original_bus, core.SignalBus = core.SignalBus, MemorySignalBus
    events, fixture_tasks, fixture_writers = [], set(), set()
    wrappers, probe = [], None
    sampler = watchdog = None
    delivery_samples = {"host": [], "guest": []}
    delivery_adjustments = []
    transport_samples = {"host": [], "guest": []}
    host = guest = server = None
    tasks = []
    cleanup_errors = []
    amount = bulk_bytes or 1_000_000
    upload = hashlib.shake_256(b"Ponte benchmark deterministic upload").digest(amount)
    download = hashlib.shake_256(b"Ponte benchmark deterministic download").digest(amount)
    upload_hash, download_hash = hashlib.sha256(upload).hexdigest(), hashlib.sha256(download).hexdigest()

    async def fixture(reader, writer):
        task = asyncio.current_task()
        fixture_tasks.add(task)
        fixture_writers.add(writer)
        try:
            operation = await reader.readexactly(1)
            if operation == b"U":
                data = await reader.readexactly(amount)
                writer.write(hashlib.sha256(data).digest())
                await writer.drain()
            elif operation == b"D":
                writer.write(download)
                await writer.drain()
            elif operation == b"P":
                while True:
                    packet = await reader.readexactly(64)
                    writer.write(packet)
                    await writer.drain()
        except (asyncio.IncompleteReadError, OSError, asyncio.CancelledError):
            pass
        finally:
            await close_writer(writer)
            fixture_writers.discard(writer)
            fixture_tasks.discard(task)

    async def send_upload():
        started = time.perf_counter()
        reader, writer = await asyncio.open_connection("127.0.0.1", guest.local_port)
        try:
            writer.write(b"U" + upload)
            await writer.drain()
            digest = (await reader.readexactly(32)).hex()
            elapsed = time.perf_counter() - started
            return {"bytes": amount, "seconds": round(elapsed, 3),
                    "mbps": round(amount * 8 / elapsed / 1e6, 3),
                    "sha256": digest, "expected_sha256": upload_hash,
                    "hash_verified": digest == upload_hash}
        finally:
            await close_writer(writer)

    async def receive_download():
        started = time.perf_counter()
        reader, writer = await asyncio.open_connection("127.0.0.1", guest.local_port)
        try:
            writer.write(b"D")
            await writer.drain()
            data = await reader.readexactly(amount)
            elapsed = time.perf_counter() - started
            digest = hashlib.sha256(data).hexdigest()
            return {"bytes": len(data), "seconds": round(elapsed, 3),
                    "mbps": round(len(data) * 8 / elapsed / 1e6, 3),
                    "sha256": digest, "expected_sha256": download_hash,
                    "hash_verified": digest == download_hash}
        finally:
            await close_writer(writer)

    async def sample_delivery():
        while True:
            for name, service in (("host", host), ("guest", guest)):
                for remote in service.peers.values():
                    window = getattr(remote, "delivery", None)
                    if window:
                        delivery_samples[name].append(window.diagnostics())
                    transport_samples[name].append({"seconds": round(time.perf_counter() - loaded_start, 3),
                                                    **sctp_diagnostics(remote)})
            await asyncio.sleep(.1)

    def snapshot(label):
        state = {"label": label, "mode": mode, "capacity_mbps_per_direction": mbps,
                 "elapsed_seconds": round(time.perf_counter() - arm_started, 3),
                 "services": {}, "tasks": []}
        for side, service in (("host", host), ("guest", guest)):
            if service:
                state["services"][side] = {"sent": service.sent, "received": service.received,
                    "peers": [{"delivery": remote.delivery.diagnostics(),
                               "streams": [{"credit": stream.credit, "pending_frames": len(stream.pending_sizes),
                                            "pending_data_bytes": sum(stream.pending_sizes),
                                            "queued_bytes": stream.queued_bytes,
                                            "buffered_amount": stream.channel.bufferedAmount,
                                            "closed": stream.closed}
                                           for stream in remote.streams]}
                              for remote in service.peers.values()]}
        for task in asyncio.all_tasks():
            state["tasks"].append({"name": task.get_name(), "coroutine": task.get_coro().__qualname__,
                                   "stack": [{"file": Path(frame.f_code.co_filename).name,
                                              "line": frame.f_lineno, "function": frame.f_code.co_name}
                                             for frame in task.get_stack(limit=4)]})
        Path(__file__).with_name("qa-transport-live-state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
        return state

    async def monitor_deadline():
        await asyncio.sleep(max_seconds)
        snapshot("measurement deadline watchdog")
        await asyncio.sleep(5)
        snapshot("five seconds after deadline watchdog")

    result = {"mode": mode, "repetition": repetition, "capacity_mbps_per_direction": mbps,
              "configured_rtt_ms": rtt_ms, "configured_packet_loss_percent": loss_percent,
              "configured_extra_jitter_ms": jitter_ms, "impairment_seed": seed,
              "baseline": baseline if mode == "baseline" else None,
              "candidate_initial_send_budget_override_bytes": candidate_window if mode == "candidate" else None,
              "initial_peer_send_budget_bytes": getattr(core, "PEER_SEND_BUDGET", None),
              "send_chunk_bytes": core.CHUNK if mode == "baseline" and baseline == "legacy" else getattr(core, "SEND_CHUNK", core.CHUNK),
              "receive_credit_window_bytes": core.WINDOW,
              "effective_stream_read_sha256": source_fingerprint(core.StreamBridge._read),
              "effective_delivery_window_sha256": source_fingerprint(core.DeliveryWindow),
              "scope": "same PC, real TCP/ICE/DTLS/SCTP; configured UDP FIFO impairment; not remote-friend evidence",
              "signaling": "authenticated real signaling codec with in-memory delivery; no HTTP", "passed": False}
    arm_started = time.perf_counter()

    def hard_exit():
        # Persist available measurements even if a future cleanup regression
        # prevents this isolated process from returning through main().
        result["hard_deadline_exceeded"] = True
        result["passed"] = False
        try:
            if checkpoint:
                checkpoint(result)
        finally:
            os._exit(3)

    hard_deadline = threading.Timer(max_seconds + 20, hard_exit)
    hard_deadline.daemon = True
    hard_deadline.start()
    restore_release = install_release_trace(core.DeliveryWindow, delivery_adjustments, arm_started)
    try:
        server = await asyncio.start_server(fixture, "127.0.0.1", 0)
        host = core.TunnelService(lambda event: None, stun=False)
        guest = core.TunnelService(events.append, stun=False)
        await host.host(server.sockets[0].getsockname()[1], "Controlled benchmark")
        await guest.join(host.code)
        await wait_for_listener(guest, events)
        peer = guest.peers["host"]
        result["route"] = peer.route()
        result["dtls_state"] = peer.pc.sctp.transport.state
        result["sctp_state"] = peer.pc.sctp.state
        for service in (host, guest):
            for remote in service.peers.values():
                for protocol in remote.pc.sctp.transport.transport._connection._protocols:
                    wrappers.append(ShapedUDP(protocol, mbps * 1e6 / 8, rtt_ms / 2000,
                                              loss_percent=loss_percent, jitter_ms=jitter_ms,
                                              seed=seed + len(wrappers)))
        result["setup_seconds"] = round(time.perf_counter() - arm_started, 3)
        reader, writer = await asyncio.open_connection("127.0.0.1", guest.local_port)
        writer.write(b"P")
        await writer.drain()
        probe = TCPProbes(reader, writer)
        probe.producer = asyncio.create_task(probe.produce(.1))
        await asyncio.sleep(1.0)
        loaded_start = time.perf_counter()
        sampler = asyncio.create_task(sample_delivery())
        watchdog = asyncio.create_task(monitor_deadline())
        tasks = [asyncio.create_task(send_upload()), asyncio.create_task(receive_download())]
        done, pending = await asyncio.wait(tasks, timeout=max_seconds)
        if not pending:
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            for name, outcome in zip(("upload", "download"), outcomes):
                result[name] = {"error": repr(outcome)} if isinstance(outcome, BaseException) else outcome
        else:
            result["bulk_timeout"] = True
            result["deadline_snapshot"] = snapshot("bulk deadline before cancellation")
            for name, task in zip(("upload", "download"), tasks):
                if task.done() and not task.cancelled():
                    exception = task.exception()
                    result[name] = {"error": repr(exception)} if exception else task.result()
                else:
                    result[name] = {"error": "measurement deadline exceeded"}
        loaded_end = time.perf_counter()
        sampler.cancel()
        await asyncio.gather(sampler, return_exceptions=True)
        result["measurement_seconds"] = round(loaded_end - loaded_start, 3)
        result["probe_drain_seconds"] = await probe.finish()
        result["idle_tcp_request_response"] = summary([rtt for start, rtt in probe.samples if start < loaded_start])
        result["loaded_tcp_request_response"] = summary([rtt for start, rtt in probe.samples if loaded_start <= start <= loaded_end])
        result["tcp_probe_series"] = probe_series(probe, arm_started, loaded_start, loaded_end)
        result["probes"] = {"bytes_request": 64, "bytes_response": 64, "period_seconds": .1,
                            "sent": probe.sent, "received": probe.received, "timeouts_after_drain": probe.timeouts,
                            "loaded_sent": sum(loaded_start <= start <= loaded_end for start in probe.started),
                            "loaded_received": sum(loaded_start <= start <= loaded_end for start, _ in probe.samples),
                            "loaded_missing_after_drain": sum(loaded_start <= start <= loaded_end for start, _ in probe.pending.values()),
                            "invalid_responses": probe.invalid,
                            "meaning": "timeouts are application probe deadlines, not observed network packet loss"}
        result["udp_shaper"] = {"datagrams": sum(w.datagrams for w in wrappers),
                                "bytes": sum(w.bytes for w in wrappers),
                                "simulated_dropped_datagrams": sum(w.dropped_datagrams for w in wrappers),
                                "simulated_dropped_bytes": sum(w.dropped_bytes for w in wrappers),
                                "transports": [{"seed": w.seed, "datagrams": w.datagrams,
                                                "bytes": w.bytes, "simulated_dropped_datagrams": w.dropped_datagrams,
                                                "simulated_dropped_bytes": w.dropped_bytes}
                                               for w in wrappers],
                                "send_errors": sum(w.send_errors for w in wrappers),
                                "maximum_fifo_queue_ms": round(max(w.maximum_queue_ms for w in wrappers), 2)}
        result["loop_lag"] = guest.diagnostics().get("event_loop_lag")
        result["sctp_loaded"] = {name: {"sample_period_seconds": .1,
                                        "extrema": series_extrema(samples), "series": samples}
                                 for name, samples in transport_samples.items()}
        result["delivery_window_loaded"] = {}
        for name, samples in delivery_samples.items():
            if samples:
                result["delivery_window_loaded"][name] = {
                    "samples": len(samples),
                    "minimum_limit_bytes": min(sample["limit_bytes"] for sample in samples),
                    "maximum_limit_bytes": max(sample["limit_bytes"] for sample in samples),
                    "maximum_admitted_bytes": max(sample["admitted_bytes"] for sample in samples),
                    "maximum_waiting_flows": max(sample["waiting_flows"] for sample in samples),
                    "final_sample": samples[-1],
                }
        result["passed"] = (all(result.get(name, {}).get("hash_verified") for name in ("upload", "download"))
                            and not probe.invalid and not probe.timeouts
                            and result["loaded_tcp_request_response"]["count"] >= 3
                            and result["dtls_state"] == "connected"
                            and not result["udp_shaper"]["send_errors"])
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        result["error"] = repr(exc)
    finally:
        # Restore before any awaited cleanup, including failed/timeout arms.
        restore_release()
        result["delivery_window_adjustments"] = delivery_adjustments
        result["measurement_passed"] = result["passed"]
        result["passed"] = False
        result["cleanup_pending"] = True
        result["cleanup_errors"] = cleanup_errors
        if checkpoint:
            checkpoint(result)

        async def cleanup_wait(label, awaitable, timeout=3):
            try:
                await asyncio.wait_for(awaitable, timeout)
            except (Exception, asyncio.CancelledError) as exc:
                cleanup_errors.append(f"{label}: {exc!r}")

        try:
            for wrapper in wrappers:
                wrapper.restore()
            # Python 3.12 Server.wait_closed() also waits for accepted
            # transports. Stop accepts, then abort fixture sockets and close
            # peers before waiting for either listener to finish closing.
            if server:
                server.close()
            for service in (guest, host):
                if service and service.server:
                    service.server.close()
            for writer in list(fixture_writers) + ([probe.writer] if probe else []):
                writer.transport.abort()
            helper_tasks = [task for task in (sampler, watchdog) if task]
            if probe:
                probe.stop.set()
                helper_tasks.extend(task for task in (probe.producer, probe.consumer) if task)
            cancelled = [*tasks, *helper_tasks, *list(fixture_tasks)]
            for task in cancelled:
                if not task.done():
                    task.cancel()
            for service in (guest, host):
                if service:
                    for peer in list(service.peers.values()):
                        await cleanup_wait(f"{service.role} peer.close", peer.close())
            if server:
                await cleanup_wait("fixture server.wait_closed", server.wait_closed())
            for service in (guest, host):
                if service:
                    await cleanup_wait(f"{service.role} service.stop", service.stop())
            await cleanup_wait("fixture/bulk/helper tasks", asyncio.gather(*cancelled, return_exceptions=True))
        except (Exception, asyncio.CancelledError) as exc:
            cleanup_errors.append(f"cleanup: {exc!r}")
        finally:
            core.SignalBus = original_bus
            restore_config()
            result["cleanup_pending"] = False
            result["arm_total_seconds"] = round(time.perf_counter() - arm_started, 3)
            result["passed"] = result["measurement_passed"] and not cleanup_errors
            if checkpoint:
                checkpoint(result)
            hard_deadline.cancel()
    print(json.dumps(result, separators=(",", ":")), flush=True)
    return result


async def main(args):
    # Python 3.12 Windows monotonic uses 15.625 ms GetTickCount64. asyncio may
    # execute timers one clock resolution early, corrupting a 20/50 ms shaper.
    # This isolated harness uses QPC for the loop and its own RTT measurements.
    loop = asyncio.get_running_loop()
    loop.time = time.perf_counter
    loop._clock_resolution = time.get_clock_info("perf_counter").resolution
    results = []
    regimes = [(2, 40), (20, 100)]
    if args.regime:
        regimes = [regimes[args.regime - 1]]
    report = {"schema": 2, "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "production_core_sha256": hashlib.sha256(Path(core.__file__).read_bytes()).hexdigest(),
              "production_flow_sha256": hashlib.sha256(Path(core.__file__).with_name("flow.py").read_bytes()).hexdigest()
                  if Path(core.__file__).with_name("flow.py").exists() else None,
              "production_send_buffer_sha256": hashlib.sha256(Path(core.__file__).with_name("send_buffer.py").read_bytes()).hexdigest()
                  if Path(core.__file__).with_name("send_buffer.py").exists() else None,
              "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "candidate_budget_constant_available": hasattr(core, "PEER_SEND_BUDGET"),
              "candidate_initial_send_budget_override_bytes": args.candidate_window,
              "candidate_uses_production_defaults": args.candidate_window is None,
              "benchmark_clock": {"implementation": time.get_clock_info("perf_counter").implementation,
                                  "resolution_seconds": loop._clock_resolution},
              "candidate_mechanism": "current production transport; explicit --candidate-window is an exploratory initial-budget override",
              "baseline": args.baseline,
              "baseline_mechanism": ("published 0.2.2 sender, DeliveryWindow and transport constants; other connection and receive code shared with candidate"
                                     if args.baseline == "0.2.2" else "frozen original _read with 256 KiB per-stream receiver credit"),
              "baseline_snapshot": {**baseline_022.SOURCE,
                                    "snapshot_file_sha256": hashlib.sha256(Path(baseline_022.__file__).read_bytes()).hexdigest()}
                                   if args.baseline == "0.2.2" else None,
              "shaping": {"model": "FIFO serialization with independent full-duplex capacity per selected ICE UDP transport; Bernoulli loss; independent extra propagation jitter",
                          "accounted_bytes": "UDP payload bytes; excludes IP/UDP headers",
                          "fixed_delay_per_direction": "configured_rtt_ms / 2",
                          "packet_loss_percent": args.loss_percent, "jitter_ms": args.jitter_ms,
                          "loss_model": "independent Bernoulli trial per attempted UDP datagram; lost packets consume serialization capacity",
                          "jitter_model": "independent uniform extra delay from 0 to jitter_ms per datagram; can reorder arrivals",
                          "seed": args.seed,
                          "seed_model": "Python random.Random; reset each arm; transport seed is base seed plus zero-based creation ordinal",
                          "queue_limit_bytes": None, "queue_drops": 0,
                          "installed_after": "authenticated ICE/DTLS/SCTP connection setup",
                          "regimes": [{"mbps_per_direction": mbps, "rtt_ms": rtt} for mbps, rtt in regimes]},
              "latency_sample_policy": "All sent probes are counted; summaries contain completed replies, including replies during a 3 s drain. Missing replies are reported and fail the arm.",
              "pending_data_bytes_meaning": "reserved or awaiting application delivery ACK; may include bytes still waiting for send-buffer admission, so it is not a measure of unsent transport queue bytes",
              "cleanup_policy": "Measurements are checkpointed before cleanup. Listeners stop accepting, fixture writers abort, peers close before listener waits, and every cleanup await has a 3 s bound.",
              "arms": results}
    output = Path(args.output).resolve()
    output_lock = threading.Lock()

    def save_report(value):
        with output_lock:
            temporary = output.with_suffix(output.suffix + ".tmp")
            temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
            temporary.replace(output)

    def checkpoint(arm):
        save_report({**report, "arms": [*results, arm], "all_passed": False,
                     "checkpoint": "arm measurement or cleanup completed; main has not yet finalized this arm"})

    for repetition in range(1, args.repetitions + 1):
        for mbps, rtt_ms in regimes:
            modes = ("baseline", "candidate") if repetition % 2 else ("candidate", "baseline")
            if args.mode:
                modes = (args.mode,)
            for mode in modes:
                results.append(await run_arm(mode, mbps, rtt_ms, args.candidate_window, repetition,
                                             args.max_seconds, args.bulk_bytes, args.baseline,
                                             args.loss_percent, args.jitter_ms, args.seed, checkpoint))
                report["all_passed"] = all(item["passed"] for item in results)
                comparisons = []
                for item in results:
                    if item["mode"] != "candidate":
                        continue
                    baseline = next((other for other in results if other["mode"] == "baseline"
                                     and other["repetition"] == item["repetition"]
                                     and other["capacity_mbps_per_direction"] == item["capacity_mbps_per_direction"]), None)
                    if baseline is None:
                        continue
                    comparison = {"repetition": item["repetition"],
                                  "capacity_mbps_per_direction": item["capacity_mbps_per_direction"],
                                  "configured_rtt_ms": item["configured_rtt_ms"]}
                    for metric in ("p50_ms", "p95_ms"):
                        before = baseline.get("loaded_tcp_request_response", {}).get(metric)
                        after = item.get("loaded_tcp_request_response", {}).get(metric)
                        if before and after is not None:
                            comparison[f"loaded_{metric}_change_percent"] = round((after / before - 1) * 100, 2)
                    for direction in ("upload", "download"):
                        before = baseline.get(direction, {}).get("mbps")
                        after = item.get(direction, {}).get("mbps")
                        if before and after is not None:
                            comparison[f"{direction}_throughput_change_percent"] = round((after / before - 1) * 100, 2)
                    comparisons.append(comparison)
                report["comparisons"] = comparisons
                save_report(report)
    return 0 if report.get("all_passed") else 1


def build_parser():
    def loss_value(value):
        number = float(value)
        if not 0 <= number <= 100:
            raise argparse.ArgumentTypeError("loss must be between 0 and 100 percent")
        return number

    def jitter_value(value):
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise argparse.ArgumentTypeError("jitter must be finite and nonnegative")
        return number

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", choices=("legacy", "0.2.2"), default="legacy",
                        help="Frozen comparison transport; default preserves the original pre-0.2.2 sender")
    parser.add_argument("--candidate-window", type=int, default=None,
                        help="Exploratory override only; omission executes production's exact initial budget")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--regime", type=int, choices=(1, 2))
    parser.add_argument("--mode", choices=("baseline", "candidate"))
    parser.add_argument("--max-seconds", type=float, default=10)
    parser.add_argument("--bulk-bytes", type=int, default=0)
    parser.add_argument("--loss-percent", type=loss_value, default=0,
                        help="Independent simulated UDP loss percent after setup; default zero")
    parser.add_argument("--jitter-ms", type=jitter_value, default=0,
                        help="Uniform extra propagation delay in 0..X ms per UDP datagram; default zero")
    parser.add_argument("--seed", type=int, default=20261024,
                        help="Deterministic impairment seed; each arm resets the same seed")
    parser.add_argument("--output", default=str(Path(__file__).parent / "qa-transport.json"))
    return parser


if __name__ == "__main__":
    arguments = build_parser().parse_args()
    logging.basicConfig(level=logging.WARNING)
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(asyncio.run(main(arguments)))
