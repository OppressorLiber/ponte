"""Room admission and invitation lifecycle over real local HTTP/RTC/TCP."""
import asyncio
import contextlib
import uuid
import unittest
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pontelan import core
from pontelan.identity import Identity, TrustStore, _trust_file_lock
from test_tunnel import LocalNtfy, wait_event
from test_lifecycle import FakeChannel, FakeWriter


class RoomControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.signaling = LocalNtfy()
        await self.signaling.start()
        self.host_events = []
        self.host = core.TunnelService(self.host_events.append, endpoint=self.signaling.url, stun=False, identity=Identity.generate(), trust_store=TrustStore.in_memory())
        self.services = [self.host]
        self.game_connections = 0
        self.game_tasks = set()
        self.game = await asyncio.start_server(self.echo, "127.0.0.1", 0)
        await self.host.host(self.game.sockets[0].getsockname()[1], "Controlled room")

    async def echo(self, reader, writer):
        self.game_connections += 1
        task = asyncio.current_task()
        self.game_tasks.add(task)
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            self.game_tasks.discard(task)

    async def asyncTearDown(self):
        for service in reversed(self.services):
            await service.stop()
        self.game.close()
        await self.game.wait_closed()
        for task in list(self.game_tasks):
            task.cancel()
        await asyncio.gather(*self.game_tasks, return_exceptions=True)
        await self.signaling.close()

    async def request(self, name="Amigo", code=None, identity=None):
        events = []
        guest = core.TunnelService(events.append, endpoint=self.signaling.url, stun=False,
                                   identity=identity or Identity.generate(), trust_store=TrustStore.in_memory())
        self.services.append(guest)
        await guest.join(code or self.host.code, name=name)
        return guest, events

    async def pending(self, guest):
        return await wait_event(self.host_events,
            lambda e: e.get("type") == "admission" and e.get("state") == "pending"
                      and e.get("peer_id") == guest.bus.peer_id, timeout=2)

    async def test_request_allocates_no_host_rtc_or_world_access_before_approval(self):
        guest, events = await self.request("Alice\n")
        pending = await self.pending(guest)
        await wait_event(events, lambda e: e.get("state") == "waiting_approval")
        await asyncio.sleep(.05)
        self.assertEqual(self.host.peers, {})
        self.assertEqual(self.game_connections, 1)  # Only host's initial port probe.
        self.assertIsNone(guest.server)
        self.assertEqual(pending["name"], "Alice")
        self.assertEqual(len(self.host.pending_requests), 1)

    async def approved(self, name="Amigo", *, remember=False, identity=None):
        guest, events = await self.request(name, identity=identity)
        await self.pending(guest)
        await self.host.approve(guest.bus.peer_id, remember=remember)
        await wait_event(events, lambda e: e.get("type") == "listening")
        return guest, events

    async def open_echo(self, guest, payload=b"direct bytes\x00\xff"):
        reader, writer = await asyncio.open_connection("127.0.0.1", guest.local_port)
        self.addAsyncCleanup(self.close_writer, writer)
        writer.write(payload)
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(len(payload)), 3), payload)
        return reader, writer

    async def close_writer(self, writer):
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()

    async def test_approval_transfers_real_bytes_and_reject_requires_fresh_request(self):
        guest, _ = await self.approved("Alice")
        await self.open_echo(guest)
        rejected, events = await self.request("Bob")
        peer_id = rejected.bus.peer_id
        await self.pending(rejected)
        duplicate = {"_sender": peer_id, "type": "offer", "name": "Bob",
                     "sdp": rejected.peers["host"].pc.localDescription.sdp}
        await self.host.reject(peer_id)
        await wait_event(events, lambda e: e.get("state") == "stopped")
        await self.host._handle_signal(duplicate)
        self.assertNotIn(peer_id, self.host.pending_requests)
        self.assertNotIn(peer_id, self.host.peers)
        with self.assertRaises(ValueError):
            await self.host.approve(peer_id)
        fresh, _ = await self.approved("Bob")
        self.assertNotEqual(fresh.bus.peer_id, peer_id)
        await self.open_echo(fresh)

    async def test_pending_expiration_does_not_extend_on_duplicate(self):
        with patch.object(core, "APPROVAL_TIMEOUT", .3):
            guest, events = await self.request()
            peer_id = guest.bus.peer_id
            await self.pending(guest)
            request = self.host.pending_requests[peer_id]
            deadline = request["expires_at"]
            await asyncio.sleep(.1)
            await self.host._handle_signal({"_sender": peer_id, "type": "offer", "name": "Changed",
                                            "sdp": guest.peers["host"].pc.localDescription.sdp})
            self.assertEqual(request["expires_at"], deadline)
            await wait_event(self.host_events, lambda e: e.get("type") == "admission"
                and e.get("peer_id") == peer_id and e.get("state") == "expired", timeout=2)
            await wait_event(events, lambda e: e.get("state") == "stopped", timeout=2)
        self.assertFalse(self.host.pending_requests)
        self.assertFalse(self.host.peers)

    async def test_pending_is_bounded_validated_and_guest_can_cancel(self):
        guest, _ = await self.request()
        await self.pending(guest)
        peer_id = guest.bus.peer_id
        sdp = guest.peers["host"].pc.localDescription.sdp
        for _ in range(core.MAX_PENDING + 2):
            await self.host._handle_signal({"_sender": uuid.uuid4().hex, "type": "offer",
                                            "sdp": sdp, "name": "x\n" * 100})
        self.assertEqual(len(self.host.pending_requests), core.MAX_PENDING)
        self.assertTrue(all(len(r["name"]) <= 60 for r in self.host.pending_requests.values()))
        self.assertFalse(self.host.peers)
        await guest.stop()
        await wait_event(self.host_events, lambda e: e.get("type") == "admission"
            and e.get("peer_id") == peer_id and e.get("state") == "cancelled")
        for bad in ("garbage", "v=0\r\n", "x" * 128001,
                    "v=0\r\na=candidate:1 1 udp 1 127.0.0.1 9 typ relay\r\n"):
            sender = uuid.uuid4().hex
            await self.host._handle_signal({"_sender": sender, "type": "offer", "sdp": bad})
            self.assertNotIn(sender, self.host.pending_requests)
        self.assertFalse(self.host.peers)

    async def test_rotation_preserves_live_tcp_cancels_pending_and_invalidates_old_invite(self):
        guest, _ = await self.approved("Alice")
        reader, writer = await self.open_echo(guest)
        waiting, waiting_events = await self.request("Waiting")
        waiting_id = waiting.bus.peer_id
        await self.pending(waiting)
        old_code, old_bus = self.host.code, self.host.bus
        code = await self.host.renew_invite()
        self.assertNotEqual(code, old_code)
        self.assertTrue(old_bus._closed)
        self.assertEqual(len(self.host.peers), 1)
        self.assertFalse(self.host.pending_requests)
        await wait_event(self.host_events, lambda e: e.get("type") == "admission"
            and e.get("peer_id") == waiting_id and e.get("state") == "cancelled")
        await wait_event(waiting_events, lambda e: e.get("state") == "stopped")
        writer.write(b"still direct after rotation")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(27), 3), b"still direct after rotation")
        old_guest, _ = await self.request("Old invite", old_code)
        await asyncio.sleep(.1)
        self.assertNotIn(old_guest.bus.peer_id, self.host.pending_requests)
        self.assertIsNone(old_guest.server)
        fresh, _ = await self.approved("New invite")
        await self.open_echo(fresh)
        # Delayed delivery and status from the retired subscription cannot
        # create requests, report errors, or close an already connected peer.
        count = len(self.host_events)
        await old_bus.on_message({"_sender": guest.bus.peer_id, "type": "bye"})
        old_bus.on_status("stale reconnect")
        await asyncio.sleep(0)
        self.assertEqual(len(self.host_events), count)
        self.assertIn(guest.bus.peer_id, self.host.peers)

    async def test_remove_closes_only_target_and_requires_new_approval(self):
        first, first_events = await self.approved("First")
        second, _ = await self.approved("Second")
        first_id = first.bus.peer_id
        first_port = first.local_port
        reader, _ = await self.open_echo(first)
        second_reader, second_writer = await self.open_echo(second)
        await self.host.remove_peer(first_id)
        await wait_event(first_events, lambda e: e.get("state") == "stopped", timeout=3)
        self.assertEqual(set(self.host.peers), {second.bus.peer_id})
        with contextlib.suppress(ConnectionError):
            self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        with self.assertRaises(OSError):
            await asyncio.open_connection("127.0.0.1", first_port)
        second_writer.write(b"unaffected")
        await second_writer.drain()
        self.assertEqual(await asyncio.wait_for(second_reader.readexactly(10), 3), b"unaffected")
        rejoining, _ = await self.request("First")
        await self.pending(rejoining)
        self.assertIsNone(rejoining.server)
        self.assertNotIn(rejoining.bus.peer_id, self.host.peers)

    async def test_rotation_start_failure_rolls_back_working_code_and_pending(self):
        connected, _ = await self.approved()
        reader, writer = await self.open_echo(connected)
        waiting, _ = await self.request("Waiting")
        await self.pending(waiting)
        old_bus, old_code = self.host.bus, self.host.code
        original = core.SignalBus
        candidates = []

        def failed_bus(*args, **kwargs):
            bus = original(*args, **kwargs)
            candidates.append(bus)

            async def fail():
                raise RuntimeError("local subscription refused")

            bus.start = fail
            return bus

        with patch.object(core, "SignalBus", side_effect=failed_bus):
            with self.assertRaisesRegex(RuntimeError, "subscription refused"):
                await self.host.renew_invite()
        self.assertIs(self.host.bus, old_bus)
        self.assertEqual(self.host.code, old_code)
        self.assertFalse(old_bus._closed)
        self.assertTrue(candidates[0]._closed)
        self.assertIn(waiting.bus.peer_id, self.host.pending_requests)
        writer.write(b"rollback")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(8), 3), b"rollback")
        self.assertEqual(self.host_events[-1].get("state"), "ready")
        await self.host.approve(waiting.bus.peer_id)

    async def test_stop_during_rotation_cannot_resurrect_room(self):
        original = core.SignalBus
        entered = asyncio.Event()
        candidates = []

        def gated_bus(*args, **kwargs):
            bus = original(*args, **kwargs)
            candidates.append(bus)

            async def gated_start():
                entered.set()
                await asyncio.Event().wait()

            bus.start = gated_start
            return bus

        with patch.object(core, "SignalBus", side_effect=gated_bus):
            renewing = asyncio.create_task(self.host.renew_invite())
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(self.host.stop(), 1)
            with self.assertRaises(asyncio.CancelledError):
                await renewing
        self.assertTrue(candidates[0]._closed)
        self.assertIsNone(self.host.bus)
        self.assertEqual(self.host.code, "")
        self.assertFalse(self.host.peers)
        self.assertFalse(self.host.pending_requests)
        await self.host.stop()

    async def test_queued_old_room_actions_cannot_change_replacement_room(self):
        for action in (lambda: self.host.renew_invite(), lambda: self.host.approve("a" * 32),
                       lambda: self.host.reject("a" * 32), lambda: self.host.remove_peer("a" * 32)):
            async with self.host.room_lock:
                queued = asyncio.create_task(action())
                await asyncio.sleep(0)
                self.assertFalse(queued.done())
                await self.host.stop()
                replacement_code = await self.host.host(self.game.sockets[0].getsockname()[1], "Replacement")
                replacement_bus = self.host.bus
            with self.assertRaisesRegex(ValueError, "mudou"):
                await queued
            self.assertEqual(self.host.code, replacement_code)
            self.assertIs(self.host.bus, replacement_bus)
            self.assertFalse(replacement_bus._closed)

    async def test_slow_approval_does_not_block_expiration_or_removal(self):
        connected, connected_events = await self.approved("Connected")
        with patch.object(core, "APPROVAL_TIMEOUT", .3):
            approving, _ = await self.request("Approving")
            await self.pending(approving)
            expiring, _ = await self.request("Expiring")
            expiring_id = expiring.bus.peer_id
            await self.pending(expiring)
            entered = asyncio.Event()

            async def gated_answer(peer, sdp):
                entered.set()
                await asyncio.Event().wait()

            with patch.object(core.TunnelPeer, "answer", gated_answer):
                approving_task = asyncio.create_task(self.host.approve(approving.bus.peer_id))
                await asyncio.wait_for(entered.wait(), 1)
                await asyncio.wait_for(self.host.remove_peer(connected.bus.peer_id), .8)
                await wait_event(connected_events, lambda e: e.get("state") == "stopped", timeout=1)
                await wait_event(self.host_events, lambda e: e.get("type") == "admission"
                    and e.get("peer_id") == expiring_id and e.get("state") == "expired", timeout=1)
                self.assertFalse(approving_task.done())
                await self.host.stop()
                with self.assertRaises(asyncio.CancelledError):
                    await approving_task

    async def test_slow_rotation_start_does_not_block_expiration_or_removal(self):
        connected, _ = await self.approved("Connected")
        with patch.object(core, "APPROVAL_TIMEOUT", .3):
            expiring, _ = await self.request("Expiring")
            peer_id = expiring.bus.peer_id
            await self.pending(expiring)
            original = core.SignalBus
            entered = asyncio.Event()

            def gated_bus(*args, **kwargs):
                bus = original(*args, **kwargs)

                async def start():
                    entered.set()
                    await asyncio.Event().wait()

                bus.start = start
                return bus

            with patch.object(core, "SignalBus", side_effect=gated_bus):
                renewing = asyncio.create_task(self.host.renew_invite())
                await asyncio.wait_for(entered.wait(), 1)
                await asyncio.wait_for(self.host.remove_peer(connected.bus.peer_id), .8)
                await wait_event(self.host_events, lambda e: e.get("type") == "admission"
                    and e.get("peer_id") == peer_id and e.get("state") == "expired", timeout=1)
                self.assertFalse(renewing.done())
                await self.host.stop()
                with self.assertRaises(asyncio.CancelledError):
                    await renewing

    async def test_remembered_key_auto_enters_next_host_instance_with_fresh_invite(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "trusted.json"
        self.host.trust_store = TrustStore(path)
        identity = Identity.generate()
        guest, _ = await self.approved("Alice", remember=True, identity=identity)
        old_code = self.host.code
        self.assertEqual((await self.host.trusted_computers())[0]["fingerprint"], identity.fingerprint)
        await guest.stop()
        await self.host.stop()
        self.host = core.TunnelService(self.host_events.append, endpoint=self.signaling.url, stun=False,
                                       identity=Identity.generate(), trust_store=TrustStore(path))
        self.services.append(self.host)
        await self.host.host(self.game.sockets[0].getsockname()[1], "New room")
        self.assertNotEqual(self.host.code, old_code)
        returning, events = await self.request("Alice", identity=identity)
        await wait_event(events, lambda e: e.get("type") == "listening")
        await self.open_echo(returning)
        pending, _ = await self.request("Alice")
        pending_event = await self.pending(pending)
        self.assertFalse(pending_event["trusted"])
        self.assertNotEqual(pending_event["fingerprint"], identity.fingerprint)
        self.assertIsNone(pending.server)

    async def test_forged_or_replayed_identity_proof_cannot_create_admission(self):
        identity = Identity.generate()
        guest, _ = await self.approved("Alice", remember=True, identity=identity)
        sdp = guest.peers["host"].pc.localDescription.sdp
        forged_sender = uuid.uuid4().hex
        proof = identity.sign_offer(self.host.code, guest.bus.peer_id, sdp, "Alice")
        with self.assertLogs(core.LOG, level="WARNING"):
            await self.host._handle_signal({"_sender": forged_sender, "type": "offer", "name": "Alice",
                                            "sdp": sdp, "identity": proof})
        self.assertNotIn(forged_sender, self.host.pending_requests)
        self.assertNotIn(forged_sender, self.host.peers)
        self.assertEqual(set(self.host.peers), {guest.bus.peer_id})

    async def test_connected_trust_toggle_and_forget_preserve_existing_bytes(self):
        identity = Identity.generate()
        guest, _ = await self.approved("Alice", identity=identity)
        peer_id = guest.bus.peer_id
        reader, writer = await self.open_echo(guest)
        await self.host.set_trusted(peer_id, True)
        self.assertEqual(len(await self.host.trusted_computers()), 1)
        await self.host.set_trusted(peer_id, False)
        self.assertEqual(await self.host.trusted_computers(), [])
        await self.host.set_trusted(peer_id, True)
        await self.host.forget_trusted(identity.fingerprint)
        self.assertEqual(await self.host.trusted_computers(), [])
        writer.write(b"still connected")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(15), 3), b"still connected")
        await guest.stop()
        returning, _ = await self.request("Alice", identity=identity)
        await self.pending(returning)
        self.assertIsNone(returning.server)

    async def test_removal_revokes_same_key_for_all_sessions_but_keeps_other_session_connected(self):
        identity = Identity.generate()
        first, first_events = await self.approved("Alice", remember=True, identity=identity)
        second, second_events = await self.request("Alice", identity=identity)
        await wait_event(second_events, lambda e: e.get("type") == "listening")
        reader, writer = await self.open_echo(second)
        await self.host.remove_peer(first.bus.peer_id)
        await wait_event(first_events, lambda e: e.get("state") == "stopped")
        self.assertEqual(await self.host.trusted_computers(), [])
        self.assertIn(second.bus.peer_id, self.host.peers)
        self.assertTrue(any(e.get("type") == "peer" and e.get("peer_id") == second.bus.peer_id
                            and e.get("trusted") is False for e in self.host_events))
        writer.write(b"other session")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(13), 3), b"other session")
        third, _ = await self.request("Alice", identity=identity)
        await self.pending(third)
        self.assertNotIn(third.bus.peer_id, self.host.peers)

    async def test_removal_still_closes_peer_and_blocks_auto_entry_if_persisting_revocation_fails(self):
        identity = Identity.generate()
        guest, events = await self.approved("Alice", remember=True, identity=identity)
        peer_id = guest.bus.peer_id
        with patch.object(self.host.trust_store, "forget", side_effect=OSError("read-only disk")):
            await self.host.remove_peer(peer_id)
        await wait_event(events, lambda e: e.get("state") == "stopped")
        self.assertNotIn(peer_id, self.host.peers)
        self.assertTrue(self.host.trust_store.is_trusted(identity.public_key))
        self.assertEqual(await self.host.trusted_computers(), [])
        self.assertTrue(any(e.get("operation") == "trust" and e.get("peer_id") == peer_id
                            for e in self.host_events))
        returning, _ = await self.request("Alice", identity=identity)
        await self.pending(returning)
        await asyncio.sleep(.05)
        self.assertIsNone(returning.server)

    async def test_corrupt_or_unreadable_trust_store_cannot_prevent_removal(self):
        identity = Identity.generate()
        guest, events = await self.approved("Alice", remember=True, identity=identity)
        peer_id = guest.bus.peer_id
        with patch.object(self.host.trust_store, "forget", side_effect=ValueError("corrupt store")), \
                patch.object(self.host.trust_store, "list", side_effect=ValueError("corrupt store")), \
                patch.object(self.host.trust_store, "is_trusted", side_effect=ValueError("corrupt store")):
            await self.host.remove_peer(peer_id)
        await wait_event(events, lambda e: e.get("state") == "stopped")
        self.assertNotIn(peer_id, self.host.peers)
        self.assertTrue(any(e.get("operation") == "trust" for e in self.host_events))

    async def test_contended_trust_file_never_blocks_rtc_and_removal_still_revokes(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "trusted.json"
        self.host.trust_store = TrustStore(path)
        identity = Identity.generate()
        guest, events = await self.approved("Alice", identity=identity)
        peer_id = guest.bus.peer_id
        reader, writer = await self.open_echo(guest)
        with _trust_file_lock(path):
            started = time.perf_counter()
            with self.assertRaises(TimeoutError):
                await self.host.set_trusted(peer_id, True)
            self.assertLess(time.perf_counter() - started, .2)
            writer.write(b"echo while trust file is locked")
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.readexactly(31), 1),
                             b"echo while trust file is locked")
        await self.host.set_trusted(peer_id, True)
        with _trust_file_lock(path):
            await asyncio.wait_for(self.host.remove_peer(peer_id), .8)
            self.assertNotIn(peer_id, self.host.peers)
            self.assertIn(identity.fingerprint, self.host.revoked_fingerprints)
            self.assertTrue(self.host.trust_store.is_trusted(identity.public_key))
            self.assertEqual(await self.host.trusted_computers(), [])
        await wait_event(events, lambda e: e.get("state") == "stopped")
        self.assertTrue(any(e.get("operation") == "trust" and e.get("peer_id") == peer_id
                            for e in self.host_events))

    async def test_trusted_auto_entry_still_respects_peer_capacity(self):
        identity = Identity.generate()
        await self.approved("Alice", remember=True, identity=identity)
        with patch.object(core, "MAX_PEERS", 1):
            other, events = await self.request("Alice", identity=identity)
            await wait_event(events, lambda e: e.get("state") == "stopped")
        self.assertEqual(len(self.host.peers), 1)
        self.assertIsNone(other.server)

    async def test_trust_revoked_while_pending_notification_is_sending_prevents_auto_entry(self):
        identity = Identity.generate()
        self.host.trust_store.remember(identity.public_key, "Alice")
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.host.bus.send

        async def gated_send(target, message):
            if message.get("type") == "hello":
                entered.set()
                await release.wait()
            await original(target, message)

        with patch.object(self.host.bus, "send", gated_send):
            guest, _ = await self.request("Alice", identity=identity)
            await asyncio.wait_for(entered.wait(), 1)
            await self.host.forget_trusted(identity.fingerprint)
            release.set()
            await asyncio.sleep(.05)
        self.assertIn(guest.bus.peer_id, self.host.pending_requests)
        self.assertNotIn(guest.bus.peer_id, self.host.peers)
        self.assertIsNone(guest.server)


class ApprovalDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_guest_waits_for_decision_then_starts_fresh_connection_deadline(self):
        service = core.TunnelService(lambda e: None, stun=False, identity=Identity.generate(), trust_store=TrustStore.in_memory())
        service.role = "guest"
        peer = core.TunnelPeer(service, "host", "Host")
        original = asyncio.wait_for
        timeouts = []

        async def observed(awaitable, timeout):
            timeouts.append(timeout)
            return await original(awaitable, timeout)

        try:
            with patch.object(core.asyncio, "wait_for", side_effect=observed):
                waiting = asyncio.create_task(peer.wait_connected())
                await asyncio.sleep(0)
                self.assertEqual(timeouts, [core.APPROVAL_TIMEOUT + 45])
                self.assertFalse(waiting.done())
                peer.answer_received.set()
                for _ in range(5):
                    await asyncio.sleep(0)
                self.assertEqual(timeouts, [core.APPROVAL_TIMEOUT + 45, 40])
                peer.ready.set()
                await waiting
        finally:
            await peer.close()


class RevocationTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_datachannel_cannot_open_world_while_peer_is_closing(self):
        service = core.TunnelService(lambda e: None, stun=False, identity=Identity.generate(), trust_store=TrustStore.in_memory())
        service.role = "host"
        entered, release = asyncio.Event(), asyncio.Event()

        async def gated_close():
            entered.set()
            await release.wait()

        pc = SimpleNamespace(on=lambda *args: None, close=gated_close)
        with patch.object(core, "RTCPeerConnection", return_value=pc):
            peer = core.TunnelPeer(service, "a" * 32, "Removed")
        peer.ready.set()
        service.peers[peer.peer_id] = peer
        closing = asyncio.create_task(peer.close())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            channel = FakeChannel()
            channel.label = "tcp:" + "b" * 32
            with patch.object(core.asyncio, "open_connection") as world:
                peer._datachannel(channel)
                await asyncio.sleep(0)
                world.assert_not_called()
            self.assertEqual(channel.readyState, "closed")
            self.assertFalse(peer.streams)
        finally:
            release.set()
            await closing

    async def test_revocation_during_tcp_open_closes_new_writer_without_pumping_bytes(self):
        owner = SimpleNamespace(sent=0, received=0)
        peer = SimpleNamespace(owner=owner, streams=set(), closed=False)
        channel = FakeChannel()
        bridge = core.StreamBridge(peer, channel)
        peer.streams.add(bridge)
        entered, release = asyncio.Event(), asyncio.Event()
        writer = FakeWriter()

        async def gated_open(*args, **kwargs):
            entered.set()
            await release.wait()
            return object(), writer

        with patch.object(core.asyncio, "open_connection", gated_open), \
                patch.object(bridge, "_read") as read, patch.object(bridge, "_write") as write:
            running = asyncio.create_task(bridge.run(1234))
            await asyncio.wait_for(entered.wait(), 1)
            peer.closed = True
            release.set()
            await running
            read.assert_not_called()
            write.assert_not_called()
        self.assertTrue(writer.closed)
        self.assertFalse(peer.streams)
