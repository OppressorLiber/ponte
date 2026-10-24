"""Controlled same-PC TCP/ICE/DTLS/SCTP A/B; never attaches to a user's room.

Signaling uses its real authenticated envelope, delivered in memory instead of
HTTP. A FIFO rate/delay shaper wraps actual ICE UDP transports after DTLS is up.
All latency probes are 64-byte TCP request/response traffic through the tunnel.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
import struct
import threading
import time

from pontelan import core
from pontelan.latency import summary


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
    """Independent FIFO capacity per selected direction, deterministic zero loss."""

    def __init__(self, protocol, rate, delay):
        self.protocol = protocol
        self.transport = protocol.transport
        self.rate, self.delay = rate, delay
        self.next_send = 0.0
        self.handles = set()
        self.closed = False
        self.bytes = self.datagrams = self.send_errors = 0
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
        holder = []

        def deliver():
            self.handles.discard(holder[0])
            if self.closed or self.transport.is_closing():
                return
            try:
                self.transport.sendto(data, addr)
            except (OSError, RuntimeError):
                self.send_errors += 1

        handle = loop.call_at(self.next_send + self.delay, deliver)
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


def install_config(mode, candidate_window):
    """Return a restore callback. Baseline disables future peer-wide send budget.

    The candidate executes the current production _read. A production module
    PEER_SEND_BUDGET constant, if present, is the supported benchmark knob.
    """
    original = core.StreamBridge._read
    changes = {}
    if mode == "baseline":
        core.StreamBridge._read = original_read
    for name in ("PEER_SEND_BUDGET",):
        if hasattr(core, name):
            changes[name] = getattr(core, name)
            setattr(core, name, candidate_window if mode == "candidate" else 1 << 30)

    def restore():
        core.StreamBridge._read = original
        for name, value in changes.items():
            setattr(core, name, value)

    return restore


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
            self.pending[self.sent] = (time.perf_counter(), packet)
            self.writer.write(packet)
            await self.writer.drain()
            try:
                await asyncio.wait_for(self.stop.wait(), interval)
            except TimeoutError:
                pass

    async def finish(self, drain=3.0):
        self.stop.set()
        if self.producer:
            await self.producer
        started = time.perf_counter()
        while self.pending and time.perf_counter() - started < drain:
            await asyncio.sleep(.01)
        self.timeouts = len(self.pending)
        self.consumer.cancel()
        await asyncio.gather(self.consumer, return_exceptions=True)
        return round(time.perf_counter() - started, 3)


async def run_arm(mode, mbps, rtt_ms, candidate_window, repetition, max_seconds, bulk_bytes=0):
    restore_config = install_config(mode, candidate_window)
    original_bus, core.SignalBus = core.SignalBus, MemorySignalBus
    events, fixture_tasks, fixture_writers = [], set(), set()
    wrappers, probe = [], None
    sampler = watchdog = None
    delivery_samples = {"host": [], "guest": []}
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

    # A cleanup regression must not leave an unattended benchmark forever.
    # This timer belongs only to this isolated benchmark process.
    hard_deadline = threading.Timer(max_seconds + 20, lambda: os._exit(3))
    hard_deadline.daemon = True
    hard_deadline.start()

    result = {"mode": mode, "repetition": repetition, "capacity_mbps_per_direction": mbps,
              "configured_rtt_ms": rtt_ms, "configured_packet_loss_percent": 0,
              "candidate_initial_send_budget_bytes": candidate_window if mode == "candidate" else None,
              "send_chunk_bytes": getattr(core, "SEND_CHUNK", core.CHUNK) if mode == "candidate" else core.CHUNK,
              "receive_credit_window_bytes": core.WINDOW,
              "scope": "same PC, real TCP/ICE/DTLS/SCTP; configured UDP FIFO impairment; not remote-friend evidence",
              "signaling": "authenticated real signaling codec with in-memory delivery; no HTTP", "passed": False}
    arm_started = time.perf_counter()
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
                    wrappers.append(ShapedUDP(protocol, mbps * 1e6 / 8, rtt_ms / 2000))
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
        result["probes"] = {"bytes_request": 64, "bytes_response": 64, "period_seconds": .1,
                            "sent": probe.sent, "received": probe.received, "timeouts_after_drain": probe.timeouts,
                            "invalid_responses": probe.invalid,
                            "meaning": "timeouts are application probe deadlines, not observed network packet loss"}
        result["udp_shaper"] = {"datagrams": sum(w.datagrams for w in wrappers),
                                "bytes": sum(w.bytes for w in wrappers),
                                "send_errors": sum(w.send_errors for w in wrappers),
                                "maximum_fifo_queue_ms": round(max(w.maximum_queue_ms for w in wrappers), 2)}
        result["loop_lag"] = guest.diagnostics().get("event_loop_lag")
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
        if sampler and not sampler.done():
            sampler.cancel()
            await asyncio.gather(sampler, return_exceptions=True)
        if probe:
            if not probe.stop.is_set():
                await probe.finish(.1)
            await close_writer(probe.writer)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for wrapper in wrappers:
            wrapper.restore()
        if server:
            server.close()
            await server.wait_closed()
        for writer in list(fixture_writers):
            writer.close()
        for service in (guest, host):
            if service:
                try:
                    await asyncio.wait_for(service.stop(), 3)
                except (TimeoutError, Exception) as exc:
                    cleanup_errors.append(repr(exc))
        for task in list(fixture_tasks):
            task.cancel()
        await asyncio.gather(*list(fixture_tasks), return_exceptions=True)
        core.SignalBus = original_bus
        restore_config()
        result["cleanup_errors"] = cleanup_errors
        result["arm_total_seconds"] = round(time.perf_counter() - arm_started, 3)
        if cleanup_errors:
            result["passed"] = False
        if watchdog:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
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
    report = {"schema": 1, "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "production_core_sha256": hashlib.sha256(Path(core.__file__).read_bytes()).hexdigest(),
              "production_flow_sha256": hashlib.sha256(Path(core.__file__).with_name("flow.py").read_bytes()).hexdigest()
                  if Path(core.__file__).with_name("flow.py").exists() else None,
              "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "candidate_budget_constant_available": hasattr(core, "PEER_SEND_BUDGET"),
              "benchmark_clock": {"implementation": time.get_clock_info("perf_counter").implementation,
                                  "resolution_seconds": loop._clock_resolution},
              "candidate_mechanism": "production _read with smaller data frames and adaptive peer-wide delivery budget; PEER_SEND_BUDGET sets initial allowance",
              "baseline_mechanism": "frozen original _read with 256 KiB per-stream receiver credit",
              "arms": results}
    output = Path(args.output).resolve()
    for repetition in range(1, args.repetitions + 1):
        for mbps, rtt_ms in regimes:
            modes = ("baseline", "candidate") if repetition % 2 else ("candidate", "baseline")
            if args.mode:
                modes = (args.mode,)
            for mode in modes:
                results.append(await run_arm(mode, mbps, rtt_ms, args.candidate_window, repetition, args.max_seconds, args.bulk_bytes))
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
                output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report.get("all_passed") else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-window", type=int, default=65536)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--regime", type=int, choices=(1, 2))
    parser.add_argument("--mode", choices=("baseline", "candidate"))
    parser.add_argument("--max-seconds", type=float, default=10)
    parser.add_argument("--bulk-bytes", type=int, default=0)
    parser.add_argument("--output", default=str(Path(__file__).parent / "qa-transport.json"))
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(asyncio.run(main(arguments)))
