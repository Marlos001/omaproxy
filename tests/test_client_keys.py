"""Named key operations preserve unrelated keys and never return raw secrets."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import client_keys
import diagnostics


class Management:
    def __init__(self):
        self.keys = ["fake-primary-secret", "fake-unmanaged-secret"]
        self.calls = []
        self.fail_readback = False
        self.fail_response = False
        self.no_mutation = False

    def __call__(self, route, method="GET", body=None):
        self.calls.append((route, method, body))
        if method == "GET":
            if self.fail_readback:
                raise RuntimeError("backend fake-primary-secret")
            return {"api-keys": self.keys.copy()}
        if not self.no_mutation:
            if method == "PATCH":
                if body["old"] not in self.keys:
                    self.keys.append(body["new"])
            elif method == "DELETE":
                value = parse_qs(urlsplit(route).query)["value"][0].strip()
                self.keys = [key for key in self.keys if key.strip() != value]
            else:
                raise AssertionError("Unexpected full-array write")
        if self.fail_response:
            raise RuntimeError("mutation secret " + repr(self.keys))
        return {"status": "ok"}


class ClientKeyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "private" / "client-keys.json"
        self.backend = Management()
        self.salt = "fake-private-management-salt"
        self.primary = self.backend.keys[0]

    def create(self, name="codex-cli"):
        return client_keys.create_key(self.backend, self.path, self.salt, name, self.primary)

    def seed(self, key, name="codex-cli"):
        client_keys._write(self.path, [{"name": name, "key_label": client_keys._label(key, self.salt), "state": "active"}])

    def test_list_is_get_only_and_does_not_assign_unmanaged_keys(self):
        self.assertEqual(client_keys.list_keys(self.backend, self.path, self.salt), {"client_keys": []})
        self.assertEqual(self.backend.calls, [(client_keys.ROUTE, "GET", None)])
        self.assertFalse(self.path.exists())

    def test_create_hash_only_private_registry_and_diagnostics_label(self):
        result = self.create()
        key = self.backend.keys[-1]
        row = result["client_key"]
        self.assertEqual(row["key_label"], diagnostics._label("client", key, self.salt.encode()))
        self.assertTrue(row["active"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        for secret in self.backend.keys:
            self.assertNotIn(secret, str(result))
            self.assertNotIn(secret, self.path.read_text())
        self.assertEqual(self.backend.keys[:2], [self.primary, "fake-unmanaged-secret"])
        writes = [(route, method, body) for route, method, body in self.backend.calls if method != "GET"]
        self.assertEqual(writes, [(client_keys.ROUTE, "PATCH", {"old": key, "new": key})])

    def test_create_same_name_is_idempotent(self):
        first = self.create()
        second = self.create()
        self.assertEqual(first, second)
        self.assertEqual(len(self.backend.keys), 3)
        self.assertEqual(sum(method == "PATCH" for _, method, _ in self.backend.calls), 1)

    def test_failed_response_applied_mutation_recovers_by_readback(self):
        self.backend.fail_response = True
        self.assertTrue(self.create()["client_key"]["active"])

    def test_lost_readback_keeps_pending_and_retry_does_not_duplicate(self):
        def api(route, method="GET", body=None):
            result = self.backend(route, method, body)
            if method == "PATCH":
                self.backend.fail_readback = True
            return result
        with self.assertRaises(ValueError) as caught:
            client_keys.create_key(api, self.path, self.salt, "t3-code")
        self.assertNotIn(self.primary, str(caught.exception))
        self.assertEqual(json.loads(self.path.read_text())["client_keys"][0]["state"], "pending")
        self.backend.fail_readback = False
        self.assertTrue(self.create("t3-code")["client_key"]["active"])
        self.assertEqual(len(self.backend.keys), 3)

    def test_unapplied_creation_retry_does_not_generate_second_key(self):
        self.backend.no_mutation = True
        for _ in range(2):
            with self.assertRaises(ValueError):
                self.create()
        self.assertEqual(sum(method == "PATCH" for _, method, _ in self.backend.calls), 1)
        client_keys.revoke_key(self.backend, self.path, self.salt, "codex-cli", self.primary)
        self.backend.no_mutation = False
        self.assertTrue(self.create()["client_key"]["active"])

    def test_revoke_preserves_primary_unmanaged_and_other_named_key(self):
        self.create("codex-cli")
        self.create("opencode")
        keep = self.backend.keys[-1]
        result = client_keys.revoke_key(self.backend, self.path, self.salt, "codex-cli", self.primary)
        self.assertTrue(result["revoked"])
        self.assertEqual(self.backend.keys, [self.primary, "fake-unmanaged-secret", keep])
        self.assertEqual([row["name"] for row in result["client_keys"]], ["opencode"])
        delete = next(call for call in self.backend.calls if call[1] == "DELETE")
        self.assertIn("?value=", delete[0])
        self.assertIsNone(delete[2])

    def test_revoke_requires_primary_protection_before_backend_call(self):
        self.create()
        self.backend.calls.clear()
        with self.assertRaisesRegex(ValueError, "protection"):
            client_keys.revoke_key(self.backend, self.path, self.salt, "codex-cli")
        self.assertFalse(self.backend.calls)

    def test_primary_revoke_and_trimmed_alias_are_protected(self):
        for key in (self.primary, " " + self.primary + " "):
            with self.subTest(key=key):
                self.seed(key)
                if key not in self.backend.keys:
                    self.backend.keys.append(key)
                with self.assertRaisesRegex(ValueError, "protected"):
                    client_keys.revoke_key(self.backend, self.path, self.salt, "codex-cli", self.primary)
        self.assertTrue(all(method == "GET" for _, method, _ in self.backend.calls))

    def test_revoke_ambiguous_trimmed_value_cannot_delete_unrelated_key(self):
        self.create()
        self.backend.keys.append(" " + self.backend.keys[-1] + " ")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            client_keys.revoke_key(self.backend, self.path, self.salt, "codex-cli", self.primary)
        self.assertFalse(any(method == "DELETE" for _, method, _ in self.backend.calls))

    def test_failed_revoke_readback_keeps_registry_for_retry(self):
        self.create()
        self.backend.no_mutation = True
        with self.assertRaisesRegex(ValueError, "not confirmed"):
            client_keys.revoke_key(self.backend, self.path, self.salt, "codex-cli", self.primary)
        self.assertEqual(len(json.loads(self.path.read_text())["client_keys"]), 1)
        self.backend.no_mutation = False
        client_keys.revoke_key(self.backend, self.path, self.salt, "codex-cli", self.primary)
        self.assertEqual(len(self.backend.keys), 2)

    def test_copy_passes_secret_only_to_stdin(self):
        self.create()
        seen = []
        def runner(args, **kwargs):
            seen.append((args, kwargs))
            return SimpleNamespace(returncode=0)
        result = client_keys.copy_key(self.backend, self.path, self.salt, "codex-cli", runner=runner)
        args, kwargs = seen[0]
        self.assertEqual(args, ["wl-copy"])
        self.assertEqual(kwargs["input"], self.backend.keys[-1])
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertNotIn(self.backend.keys[-1], str(result))

    def test_copy_failure_redacts_exception_and_stderr(self):
        self.create()
        def runner(*args, **kwargs):
            raise RuntimeError("sensitive " + kwargs["input"])
        with self.assertRaisesRegex(ValueError, "could not be copied") as caught:
            client_keys.copy_key(self.backend, self.path, self.salt, "codex-cli", runner=runner)
        self.assertNotIn(self.backend.keys[-1], str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_inactive_named_key_cannot_be_copied(self):
        self.seed("fake-removed-key")
        self.assertFalse(client_keys.list_keys(self.backend, self.path, self.salt)["client_keys"][0]["active"])
        with self.assertRaisesRegex(ValueError, "inactive"):
            client_keys.copy_key(self.backend, self.path, self.salt, "codex-cli")

    def test_invalid_names_and_salt_fail_before_backend_call(self):
        for name in ("", "../secrets", "a\nsecret", "a" * 61, " spaced", "🙂"):
            with self.assertRaises(ValueError):
                self.create(name)
        with self.assertRaises(ValueError):
            client_keys.list_keys(self.backend, self.path, b"")
        self.assertFalse(self.backend.calls)

    def test_corrupt_registry_is_preserved_and_never_echoed(self):
        self.path.parent.mkdir()
        self.path.write_text('{"secret":"fake-registry-secret"}')
        with self.assertRaises(ValueError) as caught:
            self.create()
        self.assertNotIn("fake-registry-secret", str(caught.exception))
        self.assertFalse(self.backend.calls)
        self.assertIn("fake-registry-secret", self.path.read_text())

    def test_registry_symlink_is_not_followed(self):
        target = Path(self.temp.name) / "target.json"
        target.write_text("keep")
        self.path.parent.mkdir()
        self.path.symlink_to(target)
        with self.assertRaises(ValueError):
            self.create()
        self.assertEqual(target.read_text(), "keep")

    def test_invalid_or_oversized_backend_lists_prevent_mutation(self):
        for payload in ({"api-keys": None}, {"api-keys": ["valid", None]}, {"api-keys": ["valid"] * 201}, {}, {"api-keys": [""]}):
            calls = []
            def api(route, method="GET", body=None):
                calls.append(method)
                return payload
            with self.assertRaises(ValueError):
                client_keys.create_key(api, self.path, self.salt, "agy")
            self.assertEqual(calls, ["GET"])
            self.assertFalse(self.path.exists())

    def test_registry_write_failure_prevents_backend_mutation(self):
        with patch.object(client_keys, "_write", side_effect=ValueError("safe")):
            with self.assertRaises(ValueError):
                self.create()
        self.assertTrue(all(method == "GET" for _, method, _ in self.backend.calls))


@unittest.skipUnless(os.environ.get("OMAPROXY_TEST_BINARY"), "set OMAPROXY_TEST_BINARY for isolated backend tests")
class RealClientKeyTests(unittest.TestCase):
    def test_real_append_revoke_preserves_unrelated_keys_and_config(self):
        from test_routing import isolated_backend
        with tempfile.TemporaryDirectory() as temp, isolated_backend() as (api, config):
            path = Path(temp) / "client-keys.json"
            primary = "fake-client-key"
            created = client_keys.create_key(api, path, "fake-management-key", "kiro-cli", primary)
            self.assertTrue(created["client_key"]["active"])
            keys = api(client_keys.ROUTE)["api-keys"]
            self.assertIn(primary, keys)
            self.assertEqual(len(keys), 2)
            saved = config.read_text()
            for value in ("fake-upstream-key", "fixture comment must survive", "retry comment", "unrelated comment"):
                self.assertIn(value, saved)
            client_keys.revoke_key(api, path, "fake-management-key", "kiro-cli", primary)
            self.assertEqual(api(client_keys.ROUTE)["api-keys"], [primary])
            self.assertEqual(client_keys.list_keys(api, path, "fake-management-key"), {"client_keys": []})


if __name__ == "__main__":
    unittest.main()
