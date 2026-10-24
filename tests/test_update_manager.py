"""Updater lifecycle checks with network, package verification and helper mocked."""

import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from pontelan import update_manager as updates
from pontelan.controller import Controller


class UpdateManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name)
        self.events = []
        self.busy = False
        self.info = SimpleNamespace(version="0.3.0", notes="Nova versão", size=3, sha256="unused")
        self.package = self.directory / "Ponte-0.3.0.exe"
        self.config = {"repository": "OppressorLiber/ponte", "public_key": "test key"}
        self.manager = updates.UpdateManager(self.events.append, lambda: self.busy)
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.discover = self.patches.enter_context(patch.object(updates, "discover_update", new=AsyncMock(return_value=self.info)))
        self.download = self.patches.enter_context(patch.object(updates, "download_update", new=AsyncMock(side_effect=self.complete_download)))
        self.load_config = self.patches.enter_context(patch.object(updates, "load_config", return_value=self.config))
        self.patches.enter_context(patch.object(updates, "update_directory", return_value=self.directory))
        self.verify = self.patches.enter_context(patch.object(updates, "verify_package"))
        self.helper_paths = self.directory / "job.json", self.directory / "helper.exe"
        self.prepare = self.patches.enter_context(patch.object(updates, "prepare_update", side_effect=self.prepare_ready_helper))
        self.process = Mock()
        self.process.poll.return_value = None
        self.launch = self.patches.enter_context(patch.object(updates, "launch_helper", return_value=self.process))
        self.terminate = self.patches.enter_context(patch.object(updates, "terminate_child_tree"))

    def prepare_ready_helper(self, *args):
        self.helper_paths[0].write_text(json.dumps({"token": "a" * 32}), encoding="utf-8")
        (self.directory / ("helper-ready-" + "a" * 32 + ".json")).write_text("{}", encoding="utf-8")
        return self.helper_paths

    async def complete_download(self, info, destination, progress):
        progress(0, 3)
        progress(1, 3)
        progress(3, 3)
        destination.write_bytes(b"new")
        return destination

    def states(self):
        return [event["state"] for event in self.events]

    def assert_only_update_events(self):
        self.assertTrue(self.events)
        self.assertTrue(all(event["type"] == "update" for event in self.events))

    async def test_check_automatically_downloads_new_release_while_idle(self):
        await self.manager.check()
        self.discover.assert_awaited_once_with(updates.__version__, self.config)
        self.download.assert_awaited_once()
        self.assertEqual(self.manager.package, self.package)
        self.assertEqual(self.package.read_bytes(), b"new")
        self.assertFalse(self.manager.working)
        self.assertEqual(self.states()[0:2], ["checking", "available"])
        self.assertEqual(self.states()[-1], "ready")
        self.assertEqual(self.events[-1]["version"], "0.3.0")
        self.assertIn(100, [event["progress"] for event in self.events if event["state"] == "downloading"])
        self.assert_only_update_events()

    async def test_current_version_does_not_download(self):
        self.discover.return_value = None
        await self.manager.check()
        self.assertEqual(self.states(), ["checking", "current"])
        self.download.assert_not_awaited()
        self.assertFalse(self.manager.working)
        self.assertIsNone(self.manager.package)

    async def test_active_session_defers_download_and_idle_resumes_without_rediscovery(self):
        self.busy = True
        await self.manager.check()
        self.download.assert_not_awaited()
        self.assertEqual(self.manager.info, self.info)
        self.assertEqual(self.states()[-1], "available")
        self.assertIn("sessão", self.events[-1]["message"])
        self.busy = False
        await self.manager.idle()
        self.discover.assert_awaited_once()
        self.download.assert_awaited_once()
        self.assertEqual(self.states()[-1], "ready")
        self.assertFalse(self.manager.working)

    async def test_session_starting_during_discovery_defers_download(self):
        async def discover(*args):
            self.busy = True
            return self.info
        self.discover.side_effect = discover
        await self.manager.check()
        self.download.assert_not_awaited()
        self.assertEqual(self.states()[-1], "available")
        self.assertFalse(self.manager.working)

    async def test_progress_pauses_when_session_starts_and_idle_retries(self):
        async def download(info, destination, progress):
            progress(0, 3)
            self.busy = True
            progress(1, 3)
            self.fail("Busy progress must pause the download")
        self.download.side_effect = download
        await self.manager.check()
        self.assertEqual(self.states()[-1], "available")
        self.assertIn("pausado", self.events[-1]["message"])
        self.assertIsNone(self.manager.package)
        self.assertFalse(self.manager.working)
        self.assertNotIn("error", self.states())
        self.busy = False
        self.download.side_effect = self.complete_download
        await self.manager.idle()
        self.assertEqual(self.download.await_count, 2)
        self.assertEqual(self.states()[-1], "ready")
        self.assertEqual(self.manager.package, self.package)

    async def test_valid_cached_package_is_verified_and_reused(self):
        self.package.write_bytes(b"new")
        await self.manager.check()
        self.verify.assert_called_once_with(self.package, self.info)
        self.download.assert_not_awaited()
        self.assertEqual(self.manager.package, self.package)
        self.assertEqual(self.states(), ["checking", "available", "ready"])

    async def test_invalid_cached_package_is_downloaded_again(self):
        self.package.write_bytes(b"bad")
        self.verify.side_effect = RuntimeError("Cache inválido")
        await self.manager.check()
        self.verify.assert_called_once()
        self.download.assert_awaited_once()
        self.assertEqual(self.package.read_bytes(), b"new")
        self.assertEqual(self.states()[-1], "ready")

    async def test_package_changed_after_ready_is_revalidated_and_downloaded_again(self):
        self.manager.info, self.manager.package = self.info, self.package
        self.package.write_bytes(b"bad")
        self.verify.side_effect = RuntimeError("Cache alterado")
        await self.manager.check()
        self.discover.assert_awaited_once()
        self.assertEqual(self.verify.call_count, 2)
        self.download.assert_awaited_once()
        self.assertEqual(self.package.read_bytes(), b"new")
        self.assertEqual(self.states()[-1], "ready")

    async def test_simultaneous_checks_do_not_duplicate_network_work(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def discover(*args):
            entered.set()
            await release.wait()
            return self.info
        self.discover.side_effect = discover
        first = asyncio.create_task(self.manager.check())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await self.manager.check()
            await self.manager.idle()
            self.discover.assert_awaited_once()
            self.download.assert_not_awaited()
            release.set()
            await asyncio.wait_for(first, 1)
            self.download.assert_awaited_once()
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)

    async def test_cached_package_verification_holds_working_guard(self):
        self.manager.info, self.manager.package = self.info, self.package
        self.package.write_bytes(b"bad")
        entered, release = asyncio.Event(), asyncio.Event()
        async def verify_in_thread(*args):
            entered.set()
            await release.wait()
            raise RuntimeError("Cache inválido")
        with patch.object(updates.asyncio, "to_thread", new=AsyncMock(side_effect=verify_in_thread)) as verify:
            first = asyncio.create_task(self.manager.check())
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.assertTrue(self.manager.working, "Cache verification must reserve the update operation before yielding")
                await asyncio.wait_for(self.manager.check(), 1)
                self.assertEqual(verify.await_count, 1)
                release.set()
                await asyncio.wait_for(first, 1)
                self.discover.assert_awaited_once()
                self.download.assert_awaited_once()
                self.assertFalse(self.manager.working)
            finally:
                release.set()
                await asyncio.gather(first, return_exceptions=True)

    async def test_discovery_error_stays_in_update_state_and_can_retry(self):
        self.discover.side_effect = RuntimeError("Sem conexão para atualizar")
        await self.manager.check()
        self.assertEqual(self.states(), ["checking", "error"])
        self.assertEqual(self.events[-1]["message"], "Sem conexão para atualizar")
        self.assertFalse(self.manager.working)
        self.assert_only_update_events()
        self.discover.side_effect = None
        await self.manager.check()
        self.assertEqual(self.states()[-1], "ready")

    async def test_download_error_during_check_or_idle_is_not_transport_error(self):
        for via_idle in (False, True):
            with self.subTest(via_idle=via_idle):
                self.events.clear()
                self.manager.info = self.info if via_idle else None
                self.download.side_effect = OSError("Internal file path should not become a connection error")
                await (self.manager.idle() if via_idle else self.manager.check())
                self.assertEqual(self.states()[-1], "error")
                self.assertFalse(self.manager.working)
                self.assertNotIn("Internal file", self.events[-1]["message"])
                self.assert_only_update_events()

    async def test_cancelled_check_and_idle_release_working_flag(self):
        for via_idle in (False, True):
            with self.subTest(via_idle=via_idle):
                self.events.clear()
                self.manager.info = self.info if via_idle else None
                self.download.side_effect = asyncio.CancelledError()
                with self.assertRaises(asyncio.CancelledError):
                    await (self.manager.idle() if via_idle else self.manager.check())
                self.assertFalse(self.manager.working)
                self.assertNotIn("error", self.states())

    async def test_install_refuses_active_session_before_helper_preparation(self):
        self.manager.info, self.manager.package = self.info, self.package
        self.busy = True
        await self.manager.install()
        self.prepare.assert_not_called()
        self.launch.assert_not_called()
        self.assertFalse(self.manager.installing)
        self.assertEqual(self.states(), ["ready"])
        self.assertIn("Encerre a sessão", self.events[-1]["message"])

    async def test_install_source_tree_reports_update_error(self):
        self.manager.info, self.manager.package = self.info, self.package
        with patch.object(updates.sys, "frozen", False, create=True):
            await self.manager.install()
        self.prepare.assert_not_called()
        self.launch.assert_not_called()
        self.assert_only_update_events()
        self.assertEqual(self.states(), ["error"])

    async def test_install_frozen_prepares_once_and_blocks_reentry(self):
        self.manager.info, self.manager.package = self.info, self.package
        with patch.object(updates.sys, "frozen", True, create=True):
            await self.manager.install()
            await self.manager.install()
            await self.manager.check()
        self.prepare.assert_called_once_with(self.info, self.package)
        self.launch.assert_called_once_with(*self.helper_paths)
        self.discover.assert_not_awaited()
        self.assertTrue(self.manager.installing)
        self.assertEqual(self.manager.job_path, self.helper_paths[0])
        self.assertEqual(self.states(), ["installing"])

    async def test_session_start_during_preparation_prevents_helper_launch(self):
        def prepare(*args):
            self.busy = True
            return self.directory / "job.json", self.directory / "helper.exe"
        self.prepare.side_effect = prepare
        self.manager.info, self.manager.package = self.info, self.package
        with patch.object(updates.sys, "frozen", True, create=True):
            await self.manager.install()
        self.launch.assert_not_called()
        self.assertFalse(self.manager.installing)
        self.assertEqual(self.states(), ["ready"])
        self.assertIn("sessão começou", self.events[-1]["message"])

    async def test_helper_preparation_failure_reports_error_without_transport_reset(self):
        self.manager.info, self.manager.package = self.info, self.package
        self.prepare.side_effect = RuntimeError("Arquivo alterado; baixe novamente")
        with patch.object(updates.sys, "frozen", True, create=True):
            await self.manager.install()
        self.launch.assert_not_called()
        self.assertFalse(self.manager.installing)
        self.assertEqual(self.states(), ["error"])
        self.assert_only_update_events()

    async def test_helper_exiting_before_ready_keeps_application_open(self):
        self.manager.info, self.manager.package = self.info, self.package
        def prepare_without_ready(*args):
            self.helper_paths[0].write_text(json.dumps({"token": "b" * 32}), encoding="utf-8")
            return self.helper_paths
        self.prepare.side_effect = prepare_without_ready
        self.process.poll.return_value = 1
        with patch.object(updates.sys, "frozen", True, create=True):
            await self.manager.install()
        self.assertEqual(self.states(), ["error"])
        self.assertIn("não iniciou", self.events[-1]["message"])
        self.assertFalse(self.manager.installing)
        self.assert_only_update_events()

    async def test_session_start_after_helper_launch_terminates_helper(self):
        self.manager.info, self.manager.package = self.info, self.package
        def launch(*args):
            self.busy = True
            return self.process
        self.launch.side_effect = launch
        with patch.object(updates.sys, "frozen", True, create=True):
            await self.manager.install()
        self.terminate.assert_called_once_with(self.process)
        self.assertFalse(self.manager.installing)
        self.assertEqual(self.states(), ["ready"])
        self.assertNotIn("installing", self.states())

    async def test_helper_readiness_timeout_stops_helper_before_reporting_failure(self):
        self.manager.info, self.manager.package = self.info, self.package
        def prepare_without_ready(*args):
            self.helper_paths[0].write_text(json.dumps({"token": "c" * 32}), encoding="utf-8")
            return self.helper_paths
        self.prepare.side_effect = prepare_without_ready
        real_timeout = asyncio.timeout
        with patch.object(updates.sys, "frozen", True, create=True), \
                patch.object(updates.asyncio, "timeout", side_effect=lambda seconds: real_timeout(.01)):
            await self.manager.install()
        self.terminate.assert_called_once_with(self.process)
        self.assertFalse(self.manager.installing)
        self.assertEqual(self.states(), ["error"])
        self.assert_only_update_events()

    async def test_cancel_while_waiting_for_helper_stops_helper_and_clears_installing(self):
        self.manager.info, self.manager.package = self.info, self.package
        launched = asyncio.Event()
        def prepare_without_ready(*args):
            self.helper_paths[0].write_text(json.dumps({"token": "d" * 32}), encoding="utf-8")
            return self.helper_paths
        def launch(*args):
            launched.set()
            return self.process
        self.prepare.side_effect = prepare_without_ready
        self.launch.side_effect = launch
        with patch.object(updates.sys, "frozen", True, create=True):
            installing = asyncio.create_task(self.manager.install())
            try:
                await asyncio.wait_for(launched.wait(), 1)
                installing.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await installing
            finally:
                if not installing.done():
                    installing.cancel()
                await asyncio.gather(installing, return_exceptions=True)
        self.terminate.assert_called_once_with(self.process)
        self.assertFalse(self.manager.installing)
        self.assertNotIn("installing", self.states())


class ControllerUpdateSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_during_check_preserves_one_pending_idle_resume(self):
        # Exercise the actual scheduler without creating a GUI or network thread.
        events = []
        idle = AsyncMock()
        owner = SimpleNamespace(
            event=SimpleNamespace(emit=events.append), updater=SimpleNamespace(idle=idle, installing=False),
            update_task=None, pending_update_idle=False,
        )
        owner._schedule_update = MethodType(Controller._schedule_update, owner)
        owner._update_finished = MethodType(Controller._update_finished, owner)
        entered, release = asyncio.Event(), asyncio.Event()
        async def running_check():
            entered.set()
            await release.wait()
        owner._schedule_update(running_check())
        initial = owner.update_task
        try:
            await asyncio.wait_for(entered.wait(), 1)
            for _ in range(2):
                Controller._service_event(owner, {"type": "status", "state": "stopped"})
            self.assertTrue(owner.pending_update_idle)
            idle.assert_not_awaited()
            release.set()
            await asyncio.wait_for(initial, 1)
            await asyncio.sleep(0)
            await asyncio.wait_for(owner.update_task, 1)
            idle.assert_awaited_once()
            self.assertFalse(owner.pending_update_idle)
            self.assertEqual(len(events), 2)
        finally:
            release.set()
            await asyncio.gather(initial, return_exceptions=True)
            if owner.update_task is not initial:
                await asyncio.gather(owner.update_task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
