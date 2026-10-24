"""Signed updater lifecycle checks with isolated files and fake processes.

These tests replace no real executable and start no application. Signatures,
hashes, same-directory staging, backups and readiness files are real.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pontelan import update_helper as helper
from pontelan.updates import verify_manifest


OLD_BINARY = b"MZ previous verified working application"
NEW_BINARY = b"MZ next signed working application with different bytes"
VERSION = "0.3.0"
REPOSITORY = "example/ponte"


class FakeProcess:
    pid = 19234

    def __init__(self, returncode=None):
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -1


class UpdateHelperTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ponte-helper-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.install = self.root / "portable app"
        self.install.mkdir()
        self.updates = self.root / "local app data" / "Ponte" / "updates"
        self.updates.mkdir(parents=True)
        self.target = self.install / "Ponte.exe"
        self.target.write_bytes(OLD_BINARY)
        self.package = self.updates / "Ponte-next.exe"
        self.package.write_bytes(NEW_BINARY)
        key = Ed25519PrivateKey.generate()
        self.config = {
            "repository": REPOSITORY,
            "public_key": base64.b64encode(key.public_key().public_bytes_raw()).decode(),
        }
        manifest = {
            "schema": 1, "repository": REPOSITORY, "version": VERSION,
            "notes": "Signed lifecycle test fixture.",
            "windows": {
                "url": f"https://github.com/{REPOSITORY}/releases/download/v{VERSION}/Ponte.exe",
                "size": len(NEW_BINARY), "sha256": hashlib.sha256(NEW_BINARY).hexdigest(),
            },
        }
        raw = json.dumps(manifest, separators=(",", ":")).encode()
        self.info = verify_manifest(raw, key.sign(raw), self.config)
        for name, value in (("load_config", self.config), ("update_directory", self.updates)):
            replacement = patch.object(helper, name, return_value=value)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.started = []
        self.process = FakeProcess()

    def prepare(self):
        job_path, helper_path = helper.prepare_update(
            self.info, self.package, executable=self.target, parent_pid=8765,
        )
        self.assertEqual(helper_path.read_bytes(), OLD_BINARY)
        return job_path, helper_path

    def start_ready(self, target, job_path=None, *, error=False):
        self.started.append((Path(target), job_path, error))
        self.assertEqual(Path(target).read_bytes(), NEW_BINARY)
        job = json.loads(Path(job_path).read_text(encoding="utf-8"))
        Path(job["ack"]).write_text(json.dumps({
            "token": job["token"], "version": self.info.version,
            "sha256": self.info.sha256, "pid": self.process.pid,
        }), encoding="utf-8")
        return self.process

    def apply(self, job_path, helper_path, **kwargs):
        return helper.apply_update(
            job_path, config=self.config, helper_path=helper_path,
            wait=kwargs.pop("wait", Mock()), start=kwargs.pop("start", self.start_ready),
            ready_timeout=kwargs.pop("ready_timeout", .1), **kwargs,
        )

    def test_signed_package_replaces_target_after_parent_exit_and_ack(self):
        job_path, helper_path = self.prepare()
        wait = Mock()
        self.assertTrue(self.apply(job_path, helper_path, wait=wait))
        wait.assert_called_once_with(8765)
        self.assertEqual(self.target.read_bytes(), NEW_BINARY)
        self.assertEqual((self.updates / "Ponte-previous.exe").read_bytes(), OLD_BINARY)
        self.assertEqual(self.started, [(self.target, job_path, False)])
        job = json.loads(job_path.read_text(encoding="utf-8"))
        result = json.loads(Path(job["result"]).read_text(encoding="utf-8"))
        self.assertTrue(result["updated"])
        self.assertEqual(result["version"], VERSION)
        self.assertEqual(result["sha256"], self.info.sha256)
        self.assertFalse(list(self.install.glob(".ponte-stage-*")))
        self.assertFalse(list(self.updates.glob("install-*.lock")))

    def test_failed_new_process_launch_restores_and_restarts_previous_binary(self):
        job_path, helper_path = self.prepare()
        calls = []

        def fail_then_restart(target, path, *, error=False):
            calls.append((Path(target).read_bytes(), error))
            if not error:
                raise OSError("fixture process could not start")
            return FakeProcess()

        self.assertFalse(self.apply(job_path, helper_path, start=fail_then_restart))
        self.assertEqual(self.target.read_bytes(), OLD_BINARY)
        self.assertEqual(calls, [(NEW_BINARY, False), (OLD_BINARY, True)])
        job = json.loads(job_path.read_text(encoding="utf-8"))
        self.assertFalse(json.loads(Path(job["result"]).read_text(encoding="utf-8"))["updated"])
        self.assertFalse(list(self.updates.glob("install-*.lock")))

    def test_tampered_download_does_not_touch_or_restart_original_application(self):
        job_path, helper_path = self.prepare()
        self.package.write_bytes(b"X" * len(NEW_BINARY))
        wait, start = Mock(), Mock()
        with self.assertRaises(RuntimeError):
            self.apply(job_path, helper_path, wait=wait, start=start)
        self.assertEqual(self.target.read_bytes(), OLD_BINARY)
        self.assertEqual(helper_path.read_bytes(), OLD_BINARY)
        wait.assert_not_called()
        start.assert_not_called()
        self.assertFalse(list(self.install.glob(".ponte-*")))

    def test_parent_that_has_not_exited_does_not_spawn_duplicate_application(self):
        job_path, helper_path = self.prepare()
        start = Mock()
        self.assertFalse(self.apply(
            job_path, helper_path, wait=Mock(side_effect=RuntimeError("original app still running")),
            start=start,
        ))
        self.assertEqual(self.target.read_bytes(), OLD_BINARY)
        start.assert_not_called()

    def test_failed_result_log_does_not_rollback_or_duplicate_ready_new_application(self):
        job_path, helper_path = self.prepare()
        result = Path(json.loads(job_path.read_text(encoding="utf-8"))["result"])
        original_write = Path.write_text

        def write_except_result(path, *args, **kwargs):
            if path == result:
                raise OSError("fixture result storage unavailable")
            return original_write(path, *args, **kwargs)

        with patch.object(Path, "write_text", write_except_result):
            self.assertTrue(self.apply(job_path, helper_path))
        self.assertEqual(self.target.read_bytes(), NEW_BINARY)
        self.assertEqual(self.started, [(self.target, job_path, False)])

    def test_stale_ack_from_prior_attempt_is_removed_before_new_launch(self):
        job_path, helper_path = self.prepare()
        job = json.loads(job_path.read_text(encoding="utf-8"))
        ack = Path(job["ack"])
        ack.write_text(json.dumps({
            "token": job["token"], "version": VERSION, "sha256": self.info.sha256,
            "pid": 9999,
        }), encoding="utf-8")
        new_launches, ack_at_launch = [], []

        def exited_before_ready(target, path, *, error=False):
            if not error:
                new_launches.append(Path(target).read_bytes())
                ack_at_launch.append(ack.exists())
            return FakeProcess(returncode=1)

        self.assertFalse(self.apply(job_path, helper_path, start=exited_before_ready))
        self.assertEqual(new_launches, [NEW_BINARY])
        self.assertEqual(ack_at_launch, [False])
        self.assertEqual(self.target.read_bytes(), OLD_BINARY)

    def test_ready_confirmation_requires_running_version_and_signed_target_hash(self):
        job_path, _ = self.prepare()
        job = json.loads(job_path.read_text(encoding="utf-8"))
        self.target.write_bytes(NEW_BINARY)
        with patch("pontelan.__version__", "0.2.9"), self.assertRaises(RuntimeError):
            helper.confirm_ready(job_path, executable=self.target)
        self.assertFalse(Path(job["ack"]).exists())
        with patch("pontelan.__version__", VERSION):
            helper.confirm_ready(job_path, executable=self.target)
        confirmation = json.loads(Path(job["ack"]).read_text(encoding="utf-8"))
        self.assertEqual(confirmation["token"], job["token"])
        self.assertEqual(confirmation["sha256"], self.info.sha256)
        self.assertEqual(confirmation["version"], VERSION)
        Path(job["ack"]).unlink()
        self.target.write_bytes(b"X" * len(NEW_BINARY))
        with patch("pontelan.__version__", VERSION), self.assertRaises(RuntimeError):
            helper.confirm_ready(job_path, executable=self.target)
        self.assertFalse(Path(job["ack"]).exists())

    def test_active_helper_prevents_another_helper_replacing_same_executable(self):
        job_path, helper_path = self.prepare()
        nested_start, nested_wait = Mock(), Mock()

        def parent_exit(pid):
            self.assertEqual(pid, 8765)
            with self.assertRaises((FileExistsError, RuntimeError)):
                self.apply(job_path, helper_path, wait=nested_wait, start=nested_start)
            self.assertEqual(self.target.read_bytes(), OLD_BINARY)

        self.assertTrue(self.apply(job_path, helper_path, wait=parent_exit))
        nested_wait.assert_not_called()
        nested_start.assert_not_called()
        self.assertEqual(self.target.read_bytes(), NEW_BINARY)

    def test_readonly_target_directory_fails_before_creating_helper_or_job(self):
        with patch.object(helper.tempfile, "NamedTemporaryFile", side_effect=PermissionError("read only")):
            with self.assertRaisesRegex(RuntimeError, "pasta"):
                helper.prepare_update(self.info, self.package, executable=self.target)
        self.assertEqual(self.target.read_bytes(), OLD_BINARY)
        self.assertEqual(list(self.updates.iterdir()), [self.package])

    def test_both_helper_and_application_launch_reset_frozen_environment(self):
        job_path, helper_path = self.prepare()
        inherited = {
            "PYINSTALLER_RESET_ENVIRONMENT": "0",
            "OPENSSL_MODULES": r"C:\temp\_MEItest\providers",
            "QT_PLUGIN_PATH": r"C:\temp\_MEItest\plugins",
            "QT_QPA_PLATFORM_PLUGIN_PATH": r"C:\user\independent-plugins",
            "PATH": r"C:\Windows\System32",
        }
        with patch.dict(os.environ, inherited, clear=True), patch.object(helper.sys, "_MEIPASS", r"C:\temp\_MEItest", create=True):
            with patch.object(helper.subprocess, "Popen", return_value=self.process) as popen:
                helper.launch_helper(job_path, helper_path)
                helper.start_application(self.target, job_path)
        self.assertEqual(popen.call_count, 2)
        for call in popen.call_args_list:
            env = call.kwargs["env"]
            self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")
            self.assertNotIn("OPENSSL_MODULES", env)
            self.assertNotIn("QT_PLUGIN_PATH", env)
            self.assertEqual(env["QT_QPA_PLATFORM_PLUGIN_PATH"], inherited["QT_QPA_PLATFORM_PLUGIN_PATH"])
            self.assertTrue(call.kwargs["close_fds"])


if __name__ == "__main__":
    unittest.main()
