from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import multiprocessing
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from pontelan.identity import Identity, MAX_SDP_BYTES, TrustStore, verify_offer
from pontelan.signaling import SignalBus, new_code


def mutate_trust_process(path, operation, key, started, release, completed):
    """Use separate interpreters and handles to exercise the on-disk transaction."""
    store = TrustStore(path)
    if operation == "remember":
        original_write = store._write

        def gated_write(records):
            started.set()
            if not release.wait(8):
                raise RuntimeError("Timed out waiting for test to release the save.")
            original_write(records)

        store._write = gated_write
        store.remember(key, "Other computer")
    else:
        started.set()
        store.forget(key)
    completed.set()


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.identity = Identity.generate()
        self.code = new_code()
        self.peer_id = uuid.uuid4().hex
        self.sdp = "v=0\r\na=ice-ufrag:secret\r\na=fingerprint:sha-256 AA:BB\r\n"
        self.name = "Computador de João"

    def proof(self):
        return self.identity.sign_offer(self.code, self.peer_id, self.sdp, self.name)

    def verify(self, proof):
        return verify_offer(self.code, self.peer_id, self.sdp, self.name, proof)

    def test_public_proof_roundtrip_is_stable_and_contains_no_private_key(self):
        proof = self.proof()
        self.assertEqual(set(proof), {"public_key", "signature"})
        self.assertEqual(len(proof["public_key"]), 43)
        self.assertEqual(len(proof["signature"]), 86)
        expected = hashlib.sha256(base64.urlsafe_b64decode(proof["public_key"] + "=")).hexdigest()
        self.assertEqual(self.verify(proof), {"public_key": self.identity.public_key, "fingerprint": expected})
        self.assertEqual(self.identity.fingerprint, expected)
        self.assertNotIn("PRIVATE", json.dumps(proof))
        self.assertEqual(self.proof(), proof)

    def test_signature_binds_room_session_sdp_and_display_name(self):
        proof = self.proof()
        for fields in [
            (new_code(), self.peer_id, self.sdp, self.name),
            (self.code, uuid.uuid4().hex, self.sdp, self.name),
            (self.code, self.peer_id, self.sdp + "a=candidate:attacker\r\n", self.name),
            (self.code, self.peer_id, self.sdp, "Outro computador"),
        ]:
            with self.subTest(changed=fields):
                with self.assertRaises(ValueError):
                    verify_offer(*fields, proof)
        self.assertEqual(verify_offer("\n " + self.code + " ", self.peer_id, self.sdp, self.name, proof),
                         self.verify(proof))

    def test_same_invite_holder_can_decrypt_but_cannot_impersonate_a_trusted_key(self):
        async def ignore(message):
            pass
        trusted = TrustStore.in_memory()
        trusted.remember(self.identity.public_key, self.name)
        legitimate = SignalBus(self.code, self.peer_id, False, ignore)
        attacker_host = SignalBus(self.code, "host", True, ignore)
        offer = {"type": "offer", "sdp": self.sdp, "name": self.name, "identity": self.proof()}
        intercepted = attacker_host._decode(legitimate._encode("host", offer))
        self.assertEqual(intercepted["identity"], offer["identity"])
        attacker = Identity.generate()
        forged = {"public_key": self.identity.public_key,
                  "signature": attacker.sign_offer(self.code, self.peer_id, self.sdp, self.name)["signature"]}
        with self.assertRaises(ValueError):
            self.verify(forged)
        with self.assertRaises(ValueError):
            verify_offer(self.code, self.peer_id, self.sdp + "modified", self.name, offer["identity"])
        own_proof = attacker.sign_offer(self.code, self.peer_id, self.sdp, self.name)
        self.assertFalse(trusted.is_trusted(self.verify(own_proof)["public_key"]))

    def test_proof_rejects_tampering_noncanonical_encoding_and_invalid_shapes(self):
        proof = self.proof()
        signature = bytearray(base64.urlsafe_b64decode(proof["signature"] + "=="))
        signature[-1] ^= 1
        corrupt = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        # The unused final base64 bits must be zero, even when they decode to the same bytes.
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        last = alphabet.index(proof["public_key"][-1])
        alternate = proof["public_key"][:-1] + alphabet[last | 1]
        invalid = [None, [], {}, {**proof, "private_key": "ignored?"},
                   {**proof, "signature": corrupt}, {**proof, "public_key": Identity.generate().public_key},
                   {**proof, "public_key": proof["public_key"] + "="}, {**proof, "public_key": alternate},
                   {**proof, "signature": proof["signature"] + "="}, {**proof, "signature": "é" * 86},
                   {**proof, "public_key": None}, {**proof, "signature": 42}]
        for item in invalid:
            with self.subTest(proof_type=type(item).__name__):
                with self.assertRaises(ValueError):
                    self.verify(item)

    def test_offer_bounds_invalid_types_and_utf8(self):
        invalid_fields = [
            (None, self.peer_id, self.sdp, self.name),
            (self.code, "host", self.sdp, self.name),
            (self.code, self.peer_id.upper(), self.sdp, self.name),
            (self.code, self.peer_id, "a" * (MAX_SDP_BYTES + 1), self.name),
            (self.code, self.peer_id, "é" * (MAX_SDP_BYTES // 2 + 1), self.name),
            (self.code, self.peer_id, "", self.name),
            (self.code, self.peer_id, 5, self.name),
            (self.code, self.peer_id, self.sdp, "a" * 257),
            (self.code, self.peer_id, self.sdp, "😀" * 65),
            (self.code, self.peer_id, self.sdp, "\ud800"),
            (self.code, self.peer_id, self.sdp, float("nan")),
            (self.code, self.peer_id, "\ud800", self.name),
        ]
        for fields in invalid_fields:
            with self.subTest(types=[type(value).__name__ for value in fields]):
                with self.assertRaises(ValueError):
                    self.identity.sign_offer(*fields)

    def test_persistent_key_survives_reload_and_concurrent_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identity" / "private.pem"
            with ThreadPoolExecutor(max_workers=12) as executor:
                identities = list(executor.map(lambda _: Identity.load_or_create(path), range(24)))
            self.assertEqual(len({identity.public_key for identity in identities}), 1)
            self.assertEqual(Identity.load_or_create(path).public_key, identities[0].public_key)
            self.assertEqual(list(path.parent.iterdir()), [path])
            saved = path.read_bytes()
            self.assertIn(b"PRIVATE KEY", saved)
            self.assertNotIn("PRIVATE KEY", repr(identities[0]))
            public_proof = identities[0].sign_offer(self.code, self.peer_id, self.sdp, self.name)
            self.assertEqual(Identity.load_or_create(path).sign_offer(self.code, self.peer_id, self.sdp, self.name),
                             public_proof)

    def test_bad_existing_key_is_preserved_and_never_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.pem"
            wrong_algorithm = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
            )
            for data in [b"broken", b"a" * 4097, wrong_algorithm]:
                path.write_bytes(data)
                with self.assertRaisesRegex(ValueError, "identidade"):
                    Identity.load_or_create(path)
                self.assertEqual(path.read_bytes(), data)

    def test_creation_failure_cleans_temporary_key_and_never_publishes_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.pem"
            with patch("pontelan.identity.os.link", side_effect=PermissionError("denied")):
                with self.assertRaises(PermissionError):
                    Identity.load_or_create(path)
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_default_paths_remain_inside_local_appdata(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"LOCALAPPDATA": directory}):
            identity = Identity.load_or_create()
            expected = Path(directory) / "Ponte" / "identity" / "ed25519-private.pem"
            self.assertTrue(expected.is_file())
            self.assertEqual(Identity.load_or_create(expected).public_key, identity.public_key)
            store = TrustStore()
            store.remember(identity.public_key, "Temporary computer")
            self.assertTrue((Path(directory) / "Ponte" / "trusted-computers.json").is_file())


class TrustStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "trusted.json"
        self.first = Identity.generate()
        self.second = Identity.generate()
        self.store = TrustStore(self.path)

    def test_empty_reads_are_lazy_and_do_not_create_files(self):
        self.assertEqual(self.store.list(), [])
        self.assertFalse(self.store.is_trusted(self.first.public_key))
        self.store.forget(self.first.fingerprint)
        self.assertFalse(self.path.exists())

    def test_remember_persist_rename_revoke_and_same_name_distinct_keys(self):
        self.store.remember(self.first.public_key, "Meu computador")
        self.assertTrue(TrustStore(self.path).is_trusted(self.first.public_key))
        self.assertFalse(self.store.is_trusted(self.second.public_key))
        self.store.remember(self.second.public_key, "Meu computador")
        self.store.remember(self.first.public_key, "Novo nome")
        self.assertEqual(self.store.list(), [
            {"fingerprint": self.first.fingerprint, "name": "Novo nome"},
            {"fingerprint": self.second.fingerprint, "name": "Meu computador"},
        ])
        reloaded = TrustStore(self.path)
        reloaded.forget(self.first.fingerprint)
        self.assertFalse(self.store.is_trusted(self.first.public_key))
        self.assertTrue(reloaded.is_trusted(self.second.public_key))
        self.assertNotIn("private", self.path.read_text(encoding="utf-8").lower())

    def test_corrupted_store_is_fail_closed_preserved_and_never_overwritten(self):
        for raw in [b"broken", b"[]", b'{"version":true,"computers":[]}',
                    b'{"version":1,"computers":{}}', b'{"version":0,"version":1,"computers":[]}',
                    b"\xff", b"a" * (128 * 1024 + 1)]:
            with self.subTest(raw_size=len(raw)):
                self.path.write_bytes(raw)
                for operation in [self.store.list,
                                  lambda: self.store.is_trusted(self.first.public_key),
                                  lambda: self.store.remember(self.first.public_key, "Name"),
                                  lambda: self.store.forget(self.first.fingerprint)]:
                    with self.assertRaisesRegex(ValueError, "preservado"):
                        operation()
                self.assertEqual(self.path.read_bytes(), raw)

    def test_tampered_fingerprint_duplicate_and_noncanonical_key_are_rejected(self):
        self.store.remember(self.first.public_key, "Name")
        original = json.loads(self.path.read_text(encoding="utf-8"))
        record = original["computers"][0]
        invalid_documents = [
            {"version": 1, "computers": [{**record, "fingerprint": self.second.fingerprint}]},
            {"version": 1, "computers": [record, record]},
            {"version": 1, "computers": [{**record, "public_key": record["public_key"] + "="}]},
            {"version": 1, "computers": [{**record, "name": "a" * 257}]},
        ]
        for document in invalid_documents:
            raw = json.dumps(document).encode()
            self.path.write_bytes(raw)
            with self.assertRaises(ValueError):
                self.store.list()
            self.assertEqual(self.path.read_bytes(), raw)

    def test_failed_replace_preserves_old_trust_and_cleans_temporary_files(self):
        self.store.remember(self.first.public_key, "Name")
        original = self.path.read_bytes()
        with patch("pontelan.identity.os.replace", side_effect=PermissionError("denied")):
            for operation in [lambda: self.store.remember(self.second.public_key, "Other"),
                              lambda: self.store.forget(self.first.fingerprint)]:
                with self.assertRaises(PermissionError):
                    operation()
                self.assertEqual(self.path.read_bytes(), original)
                self.assertTrue(self.store.is_trusted(self.first.public_key))
                self.assertFalse(self.store.is_trusted(self.second.public_key))
        self.assertEqual(set(self.path.parent.iterdir()), {self.path, self.path.with_name(self.path.name + ".lock")})

    def test_store_limit_allows_rename_and_bounds_new_entries(self):
        with patch("pontelan.identity.MAX_TRUSTED_COMPUTERS", 1):
            self.store.remember(self.first.public_key, "Name")
            self.store.remember(self.first.public_key, "Renamed")
            original = self.path.read_bytes()
            with self.assertRaisesRegex(ValueError, "Limite"):
                self.store.remember(self.second.public_key, "Other")
            self.assertEqual(self.path.read_bytes(), original)

    def test_in_memory_store_never_writes_and_list_is_a_copy(self):
        with patch("pontelan.identity._write_temporary", side_effect=AssertionError("disk write")):
            store = TrustStore.in_memory()
            store.remember(self.first.public_key, "Name")
            store.list()[0]["name"] = "Changed externally"
            self.assertEqual(store.list()[0]["name"], "Name")
            self.assertTrue(store.is_trusted(self.first.public_key))
            store.forget(self.first.fingerprint)
            self.assertEqual(store.list(), [])

    def test_invalid_keys_names_and_fingerprints_are_rejected_without_writing(self):
        for operation in [lambda: self.store.remember("invalid", "Name"),
                          lambda: self.store.remember(self.first.public_key, "😀" * 65),
                          lambda: self.store.is_trusted(None),
                          lambda: self.store.forget("short"), lambda: self.store.forget(None)]:
            with self.assertRaises(ValueError):
                operation()
        self.assertFalse(self.path.exists())

    def test_concurrent_instances_cannot_restore_a_revoked_key(self):
        self.store.remember(self.first.public_key, "To revoke")
        saving, revoking = TrustStore(self.path), TrustStore(self.path)
        save_started, release_save, revoke_started = threading.Event(), threading.Event(), threading.Event()
        original_write = saving._write

        def gated_write(records):
            save_started.set()
            if not release_save.wait(5):
                raise RuntimeError("Timed out waiting for test to release the save.")
            original_write(records)

        def revoke():
            revoke_started.set()
            revoking.forget(self.first.fingerprint)

        with ThreadPoolExecutor(max_workers=2) as executor, patch.object(saving, "_write", gated_write):
            save = executor.submit(saving.remember, self.second.public_key, "Other computer")
            try:
                self.assertTrue(save_started.wait(3))
                removal = executor.submit(revoke)
                self.assertTrue(revoke_started.wait(3))
                with self.assertRaises(TimeoutError):
                    removal.result(timeout=0.2)
            finally:
                release_save.set()
            save.result(timeout=3)
            removal.result(timeout=3)
        self.assertFalse(self.store.is_trusted(self.first.public_key))
        self.assertTrue(self.store.is_trusted(self.second.public_key))

    def test_concurrent_processes_cannot_restore_a_revoked_key(self):
        self.store.remember(self.first.public_key, "To revoke")
        context = multiprocessing.get_context("spawn")
        save_started, release_save, save_done = context.Event(), context.Event(), context.Event()
        revoke_started, revoke_done = context.Event(), context.Event()
        saving = context.Process(target=mutate_trust_process, args=(
            str(self.path), "remember", self.second.public_key, save_started, release_save, save_done,
        ))
        revoking = context.Process(target=mutate_trust_process, args=(
            str(self.path), "forget", self.first.fingerprint, revoke_started, release_save, revoke_done,
        ))
        processes = []
        try:
            saving.start()
            processes.append(saving)
            self.assertTrue(save_started.wait(5))
            revoking.start()
            processes.append(revoking)
            self.assertTrue(revoke_started.wait(5))
            self.assertFalse(revoke_done.wait(0.2), "Revocation must wait for the other transaction.")
        finally:
            release_save.set()
            for process in processes:
                process.join(timeout=8)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=3)
        self.assertTrue(save_done.is_set())
        self.assertTrue(revoke_done.is_set())
        self.assertEqual([process.exitcode for process in processes], [0, 0])
        self.assertFalse(self.store.is_trusted(self.first.public_key))
        self.assertTrue(self.store.is_trusted(self.second.public_key))

    def test_busy_transaction_times_out_without_modifying_the_allowlist(self):
        self.store.remember(self.first.public_key, "Keep trusted")
        original = self.path.read_bytes()
        competing = TrustStore(self.path)
        with self.store._transaction(), patch("pontelan.identity._LOCK_TIMEOUT", 0.05):
            with self.assertRaisesRegex(TimeoutError, "está em uso"):
                competing.forget(self.first.fingerprint)
            self.assertEqual(self.path.read_bytes(), original)
        competing.forget(self.first.fingerprint)
        self.assertFalse(self.store.is_trusted(self.first.public_key))

    def test_nonblocking_mutations_never_sleep_when_another_instance_holds_the_lock(self):
        self.store.remember(self.first.public_key, "Keep trusted")
        original = self.path.read_bytes()
        competing = TrustStore(self.path)
        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.store._transaction(), patch("pontelan.identity.time.sleep", side_effect=AssertionError("slept")):
                for operation in [lambda: competing.remember(self.second.public_key, "Other", timeout=0),
                                  lambda: competing.forget(self.first.fingerprint, timeout=0)]:
                    with self.assertRaisesRegex(TimeoutError, "está em uso"):
                        operation()
                    self.assertEqual(self.path.read_bytes(), original)
                # The same instance must also avoid waiting for a different thread's local mutex.
                own_instance = executor.submit(self.store.forget, self.first.fingerprint, timeout=0)
                with self.assertRaisesRegex(TimeoutError, "está em uso"):
                    own_instance.result(timeout=0.5)
                self.assertEqual(self.path.read_bytes(), original)
        competing.remember(self.second.public_key, "Other", timeout=0)
        competing.forget(self.first.fingerprint, timeout=0)
        self.assertFalse(self.store.is_trusted(self.first.public_key))
        self.assertTrue(self.store.is_trusted(self.second.public_key))

    def test_transaction_lock_remains_stable_across_json_replacements(self):
        self.store.remember(self.first.public_key, "First")
        lock_path = self.path.with_name(self.path.name + ".lock")
        lock_inode = lock_path.stat().st_ino
        self.store.remember(self.second.public_key, "Second")
        self.store.forget(self.first.fingerprint)
        self.assertEqual(lock_path.stat().st_ino, lock_inode)
        # Readers need only the atomically replaced JSON and never create a lock.
        lock_path.unlink()
        self.assertTrue(self.store.is_trusted(self.second.public_key))
        self.assertFalse(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
