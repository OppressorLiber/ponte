from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import aiohttp
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pontelan.updates import (
    MAX_MANIFEST_BYTES, MAX_PACKAGE_BYTES, UpdateError, discover_update,
    download_update, semver_compare, verify_manifest,
)


REPOSITORY = "OppressorLiber/ponte"
VERSION = "0.2.0"
BASE = f"https://github.com/{REPOSITORY}/releases/download/v{VERSION}/"
API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
PAYLOAD = b"MZ" + b"test executable bytes" * 8000


class FakeContent:
    def __init__(self, body, failure=None):
        self.body = body
        self.failure = failure

    async def iter_chunked(self, size):
        for start in range(0, len(self.body), size):
            await asyncio.sleep(0)
            yield self.body[start:start + size]
        if self.failure:
            raise self.failure


class FakeResponse:
    def __init__(self, body=b"", status=200, headers=None, content_length=None, failure=None):
        self.status = status
        self.headers = headers or {}
        self.content_length = content_length
        self.content = FakeContent(body, failure)
        self.exited = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.exited = True


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        response = self.responses[url]
        if isinstance(response, BaseException):
            raise response
        return response


class UpdateFixture:
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.config = {"repository": REPOSITORY, "public_key": base64.b64encode(self.key.public_key().public_bytes_raw()).decode()}
        self.manifest = {
            "schema": 1, "repository": REPOSITORY, "version": VERSION,
            "notes": "Correções e atualização automática.",
            "windows": {"url": BASE + "Ponte.exe", "size": len(PAYLOAD), "sha256": hashlib.sha256(PAYLOAD).hexdigest()},
        }
        self.raw, self.signature = self.signed()
        self.info = verify_manifest(self.raw, self.signature, self.config, VERSION)

    def signed(self, manifest=None):
        raw = json.dumps(manifest or self.manifest, ensure_ascii=False, separators=(",", ":")).encode()
        return raw, self.key.sign(raw)

    def release(self, **changes):
        return {
            "draft": False, "prerelease": False, "tag_name": "v" + VERSION,
            "body": "Unsigned notes are ignored",
            "assets": [{"name": name, "browser_download_url": BASE + name} for name in ("update.json", "update.sig", "Ponte.exe")],
            **changes,
        }

    def session(self, release=None):
        return FakeSession({
            API: FakeResponse(json.dumps(self.release() if release is None else release).encode()),
            BASE + "update.json": FakeResponse(self.raw),
            BASE + "update.sig": FakeResponse(base64.b64encode(self.signature) + b"\n"),
            BASE + "Ponte.exe": FakeResponse(PAYLOAD, content_length=len(PAYLOAD)),
        })


class ManifestTests(UpdateFixture, unittest.TestCase):
    def test_valid_manifest_and_numeric_versions(self):
        self.assertEqual(self.info.notes, self.manifest["notes"])
        self.assertEqual(self.info.manifest_bytes, self.raw)
        self.assertEqual(self.info.signature, self.signature)
        self.assertEqual(semver_compare("1.10.0", "1.9.99"), 1)
        self.assertEqual(semver_compare("1.0.0", "2.0.0"), -1)
        self.assertEqual(semver_compare("0.2.0", "0.2.0"), 0)
        for invalid in ("01.2.0", "v0.2.0", "0.2", "0.2.0-beta", "0.2.0+build", " 0.2.0", True):
            with self.subTest(version=invalid), self.assertRaises(UpdateError):
                semver_compare(invalid, "0.1.0")

    def test_signature_is_over_exact_bytes_and_correct_key(self):
        wrong = Ed25519PrivateKey.generate()
        cases = [(self.raw + b" ", self.signature), (self.raw, wrong.sign(self.raw)), (self.raw, b"x" * 63)]
        for raw, signature in cases:
            with self.subTest(signature_len=len(signature)), self.assertRaises(UpdateError):
                verify_manifest(raw, signature, self.config)

    def test_no_missing_or_invalid_trust_anchor(self):
        for config in ({}, {"repository": REPOSITORY}, {**self.config, "public_key": "!"}, {**self.config, "repository": "owner/.."}):
            with self.subTest(config=config), self.assertRaises(UpdateError):
                verify_manifest(self.raw, self.signature, config)

    def test_signed_invalid_manifest_fields_are_rejected(self):
        mutations = [
            {"schema": True}, {"schema": 2}, {"repository": "someone/else"},
            {"version": "0.2.0-beta"}, {"version": "0.3.0"}, {"notes": []}, {"windows": []},
            {"windows": {**self.manifest["windows"], "size": True}},
            {"windows": {**self.manifest["windows"], "size": 0}},
            {"windows": {**self.manifest["windows"], "size": MAX_PACKAGE_BYTES + 1}},
            {"windows": {**self.manifest["windows"], "sha256": "A" * 64}},
        ]
        for changes in mutations:
            raw, signature = self.signed({**self.manifest, **changes})
            with self.subTest(changes=changes), self.assertRaises(UpdateError):
                verify_manifest(raw, signature, self.config, VERSION)

    def test_signed_external_noncanonical_and_traversal_urls_are_rejected(self):
        for url in (
            BASE.replace("https:", "http:") + "Ponte.exe", BASE + "Ponte.exe?download=1",
            BASE + "Ponte.exe#fragment", BASE + "../Ponte.exe", BASE + "%50onte.exe",
            BASE.replace("github.com", "evil.example") + "Ponte.exe",
            BASE.replace("github.com", "user:secret@github.com") + "Ponte.exe",
            BASE.replace("github.com", "github.com:443") + "Ponte.exe",
            BASE.replace("OppressorLiber/ponte", "other/repo") + "Ponte.exe",
        ):
            raw, signature = self.signed({**self.manifest, "windows": {**self.manifest["windows"], "url": url}})
            with self.subTest(url=url), self.assertRaises(UpdateError):
                verify_manifest(raw, signature, self.config)

    def test_duplicate_json_keys_and_oversized_manifest_are_rejected(self):
        raw = self.raw.replace(b'"schema":1', b'"schema":1,"schema":1')
        with self.assertRaises(UpdateError):
            verify_manifest(raw, self.key.sign(raw), self.config)
        raw = b" " * (MAX_MANIFEST_BYTES + 1)
        with self.assertRaises(UpdateError):
            verify_manifest(raw, self.key.sign(raw), self.config)


class NetworkTests(UpdateFixture, unittest.IsolatedAsyncioTestCase):
    async def test_discovery_verifies_signed_notes_and_explicit_request_policy(self):
        session = self.session()
        info = await discover_update("0.1.0", self.config, session)
        self.assertEqual(info, self.info)
        self.assertEqual(len(session.requests), 3)
        for _, kwargs in session.requests:
            self.assertFalse(kwargs["allow_redirects"])
            self.assertFalse(kwargs["auto_decompress"])
            self.assertEqual(kwargs["max_line_size"], 65536)
            self.assertEqual(kwargs["max_field_size"], 65536)
            self.assertNotIn("ssl", kwargs)
            self.assertNotIn("Authorization", kwargs["headers"])
            self.assertIsNotNone(kwargs["timeout"].total)

    async def test_equal_or_older_release_and_404_mean_no_update(self):
        for current in (VERSION, "1.0.0"):
            session = self.session()
            self.assertIsNone(await discover_update(current, self.config, session))
            self.assertEqual(len(session.requests), 1)
        self.assertIsNone(await discover_update("0.1.0", self.config, FakeSession({API: FakeResponse(status=404)})))

    async def test_drafts_prereleases_bad_tags_and_foreign_assets_are_rejected(self):
        cases = [{"draft": True}, {"prerelease": True}, {"tag_name": "v0.2.0-beta"}, {"tag_name": "0.2.0"}, {"assets": []}]
        foreign = self.release()["assets"]
        foreign[0] = {**foreign[0], "browser_download_url": "https://evil.example/update.json"}
        cases.append({"assets": foreign})
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(UpdateError):
                await discover_update("0.1.0", self.config, self.session(self.release(**changes)))

    async def test_asset_signature_tamper_and_mismatched_release_version_are_rejected(self):
        session = self.session()
        session.responses[BASE + "update.json"] = FakeResponse(self.raw + b" ")
        with self.assertRaisesRegex(UpdateError, "assinatura"):
            await discover_update("0.1.0", self.config, session)
        raw, signature = self.signed({**self.manifest, "version": "0.3.0"})
        session = self.session()
        session.responses[BASE + "update.json"] = FakeResponse(raw)
        session.responses[BASE + "update.sig"] = FakeResponse(base64.b64encode(signature))
        with self.assertRaisesRegex(UpdateError, "versão"):
            await discover_update("0.1.0", self.config, session)

    async def test_size_caps_signature_format_and_friendly_network_errors(self):
        for name, response in (
            ("update.json", FakeResponse(b"x" * (MAX_MANIFEST_BYTES + 1))),
            ("update.json", FakeResponse(b"", content_length=MAX_MANIFEST_BYTES + 1)),
            ("update.sig", FakeResponse(b"x" * 513)),
            ("update.sig", FakeResponse(b"not base64!")),
        ):
            session = self.session()
            session.responses[BASE + name] = response
            with self.subTest(name=name), self.assertRaises(UpdateError):
                await discover_update("0.1.0", self.config, session)
        for error, message in ((aiohttp.ClientConnectionError("offline"), "conexão"), (asyncio.TimeoutError(), "conexão")):
            with self.assertRaisesRegex(UpdateError, message):
                await discover_update("0.1.0", self.config, FakeSession({API: error}))
        for status in (403, 429):
            with self.assertRaisesRegex(UpdateError, "limite temporário"):
                await discover_update("0.1.0", self.config, FakeSession({API: FakeResponse(status=status)}))

    async def test_download_redirect_progress_and_atomic_replace(self):
        redirected = "https://release-assets.githubusercontent.com/github-production-release-asset/file?signature=secret"
        session = FakeSession({
            self.info.url: FakeResponse(status=302, headers={"Location": redirected}),
            redirected: FakeResponse(PAYLOAD, content_length=len(PAYLOAD)),
        })
        progress = []
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "Ponte.exe"
            path.write_bytes(b"previous")
            result = await download_update(self.info, path, lambda done, total: progress.append((done, total)), session)
            self.assertEqual(result, path)
            self.assertEqual(path.read_bytes(), PAYLOAD)
            self.assertEqual(list(Path(folder).glob("*.part")), [])
        self.assertEqual(progress[0], (0, len(PAYLOAD)))
        self.assertEqual(progress[-1], (len(PAYLOAD), len(PAYLOAD)))
        self.assertEqual([done for done, _ in progress], sorted(done for done, _ in progress))

    async def test_bad_redirects_are_rejected_before_request(self):
        for url in ("https://evil.example/Ponte.exe", "http://github.com/file", "https://user:secret@github.com/file", "https://github.com:444/file"):
            session = FakeSession({self.info.url: FakeResponse(status=302, headers={"Location": url})})
            with tempfile.TemporaryDirectory() as folder:
                with self.subTest(url=url), self.assertRaises(UpdateError):
                    await download_update(self.info, Path(folder) / "Ponte.exe", session=session)
                self.assertEqual(len(session.requests), 1)

    async def test_redirect_loop_is_bounded(self):
        session = FakeSession({self.info.url: FakeResponse(status=302, headers={"Location": self.info.url})})
        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(UpdateError, "redirecionou"):
            await download_update(self.info, Path(folder) / "Ponte.exe", session=session)
        self.assertEqual(len(session.requests), 6)

    async def test_hash_short_overlong_and_header_mismatch_preserve_destination(self):
        cases = [
            FakeResponse(b"x" * len(PAYLOAD)), FakeResponse(PAYLOAD[:-1]), FakeResponse(PAYLOAD + b"x"),
            FakeResponse(PAYLOAD, content_length=len(PAYLOAD) + 1),
            FakeResponse(PAYLOAD[:100], failure=aiohttp.ClientConnectionError("connection lost")),
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "Ponte.exe"
            for response in cases:
                path.write_bytes(b"previous")
                with self.assertRaises(UpdateError):
                    await download_update(self.info, path, session=FakeSession({self.info.url: response}))
                self.assertEqual(path.read_bytes(), b"previous")
                self.assertEqual(list(Path(folder).glob("*.part")), [])

    async def test_cancellation_cleans_part_file_and_preserves_destination(self):
        def cancel(done, total):
            if done:
                raise asyncio.CancelledError()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "Ponte.exe"
            path.write_bytes(b"previous")
            with self.assertRaises(asyncio.CancelledError):
                await download_update(self.info, path, cancel, self.session())
            self.assertEqual(path.read_bytes(), b"previous")
            self.assertEqual(list(Path(folder).glob("*.part")), [])

    async def test_invalid_download_info_never_makes_a_request(self):
        for info in (
            replace(self.info, url="http://github.com/file"), replace(self.info, size=True),
            replace(self.info, sha256=None), replace(self.info, url=self.info.url + "?x=1"),
            replace(self.info, url=BASE.replace("OppressorLiber/ponte", "owner/..") + "Ponte.exe"),
        ):
            session = FakeSession({})
            with tempfile.TemporaryDirectory() as folder:
                with self.subTest(info=info), self.assertRaises(UpdateError):
                    await download_update(info, Path(folder) / "Ponte.exe", session=session)
                self.assertEqual(session.requests, [])


if __name__ == "__main__":
    unittest.main()
