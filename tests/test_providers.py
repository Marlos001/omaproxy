"""Provider mutations target exact definitions and never expose secrets."""
import copy
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
import urllib.error
import urllib.parse

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import providers


class Management:
    def __init__(self):
        self.calls = []
        self.entries = [{"name": "first", "base-url": "https://example.test/v1",
                         "api-key-entries": [{"api-key": "fake-secret", "weight": 4, "proxy-url": "http://proxy.test"}],
                         "models": [{"name": "model-a", "alias": "alias-a", "thinking": {"levels": ["low"]}}],
                         "headers": {"private-header": "fake-header-secret"}, "disabled": True},
                        {"name": "second", "base-url": "https://other.test/v1",
                         "api-key-entries": [{"api-key": "other-secret"}], "models": []}]

    def __call__(self, route, method="GET", body=None):
        self.calls.append((route, method, copy.deepcopy(body)))
        if method == "GET":
            return {"openai-compatibility": copy.deepcopy(self.entries)}
        if method == "PATCH":
            target = next(entry for entry in self.entries if entry["name"] == body["name"])
            target.update(copy.deepcopy(body["value"]))
        elif method == "PUT":
            self.entries = copy.deepcopy(body)
        elif method == "DELETE":
            name = urllib.parse.parse_qs(urllib.parse.urlsplit(route).query)["name"][0]
            self.entries = [entry for entry in self.entries if entry["name"] != name]
        else:
            raise AssertionError(method)
        return {"status": "ok"}


class ProviderTests(unittest.TestCase):
    def test_listing_is_allowlisted(self):
        backend = Management()
        result = providers.list_providers(backend)
        rendered = json.dumps(result)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("headers", rendered)
        self.assertNotIn("proxy-url", rendered)
        self.assertEqual(result["custom_providers"][0]["credentials"][0]["weight"], 4)
        self.assertEqual(len(backend.calls), 1)

    def test_bad_config_url_is_hidden(self):
        backend = Management()
        backend.entries[0]["base-url"] = "https://user:fake-secret@example.test/v1"
        self.assertEqual(providers.list_providers(backend)["custom_providers"][0]["url"], "")

    def test_edit_uses_patch_and_blank_key_preserves_all_secrets(self):
        backend = Management()
        before = copy.deepcopy(backend.entries)
        result = providers.upsert_provider(backend, {"name": "first", "url": "https://new.test/v1", "key": ""})
        write = next(call for call in backend.calls if call[1] == "PATCH")
        self.assertEqual(write[2], {"name": "first", "value": {"base-url": "https://new.test/v1"}})
        self.assertEqual(backend.entries[0]["api-key-entries"], before[0]["api-key-entries"])
        self.assertEqual(backend.entries[1], before[1])
        self.assertNotIn("fake-secret", str(result))

    def test_edit_models_preserves_selected_model_capabilities(self):
        backend = Management()
        providers.upsert_provider(backend, {"name": "first", "models": [{"name": "model-a", "alias": "alias-a"}, "model-b"]})
        self.assertEqual(backend.entries[0]["models"][0]["thinking"], {"levels": ["low"]})

    def test_key_replacement_preserves_weight_proxy_and_unrelated_provider(self):
        backend = Management()
        providers.upsert_provider(backend, {"name": "first", "key": "new-fake-secret"})
        self.assertEqual(backend.entries[0]["api-key-entries"],
                         [{"api-key": "new-fake-secret", "weight": 4, "proxy-url": "http://proxy.test"}])
        self.assertEqual(backend.entries[1]["api-key-entries"][0]["api-key"], "other-secret")

    def test_weight_zero_and_reset_preserve_key(self):
        backend = Management()
        providers.upsert_provider(backend, {"name": "first", "weight": 0}, weights_supported=True)
        self.assertEqual(backend.entries[0]["api-key-entries"][0]["weight"], 0)
        providers.upsert_provider(backend, {"name": "first", "weight": None}, weights_supported=True)
        self.assertNotIn("weight", backend.entries[0]["api-key-entries"][0])
        self.assertEqual(backend.entries[0]["api-key-entries"][0]["api-key"], "fake-secret")

    def test_multi_key_edit_requires_explicit_index(self):
        backend = Management()
        backend.entries[0]["api-key-entries"].append({"api-key": "fake-second-key"})
        with self.assertRaisesRegex(ValueError, "which"):
            providers.upsert_provider(backend, {"name": "first", "key": "new-key"})
        providers.upsert_provider(backend, {"name": "first", "key": "new-key", "credential_index": 1})
        self.assertEqual(backend.entries[0]["api-key-entries"][0]["api-key"], "fake-secret")
        self.assertEqual(backend.entries[0]["api-key-entries"][1]["api-key"], "new-key")

    def test_add_preserves_existing_definitions(self):
        backend = Management()
        before = copy.deepcopy(backend.entries)
        providers.upsert_provider(backend, {"name": "added", "url": "https://new.test/v1", "key": "new-key", "models": "model-new"})
        self.assertEqual(backend.entries[:2], before)
        self.assertEqual([method for _, method, _ in backend.calls], ["GET", "PUT", "GET"])

    def test_keyless_local_add(self):
        backend = Management()
        providers.upsert_provider(backend, {"name": "local", "url": "http://127.0.0.1:9000/v1", "key": "", "models": "local-model"})
        self.assertEqual(backend.entries[-1]["api-key-entries"], [])

    def test_remote_add_requires_key_even_with_weight(self):
        for extra in ({}, {"weight": 2}):
            backend = Management()
            with self.assertRaisesRegex(ValueError, "API key"):
                providers.upsert_provider(backend, {"name": "new", "url": "https://new.test/v1", "models": "model", **extra}, weights_supported=True)
            self.assertTrue(all(method == "GET" for _, method, _ in backend.calls))

    def test_keyless_local_edit_to_remote_requires_effective_key(self):
        for extra in ({}, {"weight": 2}):
            backend = Management()
            backend.entries[0].update({"base-url": "http://localhost:9000/v1", "api-key-entries": []})
            with self.assertRaisesRegex(ValueError, "API key"):
                providers.upsert_provider(backend, {"name": "first", "url": "https://new.test/v1", **extra}, weights_supported=True)
            self.assertTrue(all(method == "GET" for _, method, _ in backend.calls))
        providers.upsert_provider(backend, {"name": "first", "url": "https://new.test/v1", "key": "new-key"})
        self.assertEqual(backend.entries[0]["api-key-entries"][0]["api-key"], "new-key")

    def test_create_only_cannot_edit_existing_provider(self):
        backend = Management()
        with self.assertRaisesRegex(ValueError, "already exists"):
            providers.upsert_provider(backend, {"name": "first", "key": "new-key"}, create_only=True)
        self.assertTrue(all(method == "GET" for _, method, _ in backend.calls))

    def test_remove_only_exact_target(self):
        backend = Management()
        second = copy.deepcopy(backend.entries[1])
        providers.remove_provider(backend, "first")
        self.assertEqual(backend.entries, [second])
        self.assertEqual(next(call for call in backend.calls if call[1] == "DELETE")[0], providers.ROUTE + "?name=first")

    def test_unknown_remove_and_duplicate_names_do_not_write(self):
        for duplicate in (False, True):
            backend = Management()
            if duplicate:
                backend.entries.append(copy.deepcopy(backend.entries[0]))
            with self.assertRaises(ValueError):
                providers.remove_provider(backend, "first" if duplicate else "absent")
            self.assertTrue(all(method == "GET" for _, method, _ in backend.calls))

    def test_url_rejects_remote_http_credentials_query_fragment_and_controls(self):
        for url in ("http://example.test/v1", "https://user:key@example.test", "https://example.test?key=x",
                    "https://example.test#fragment", "https://example.test\\evil", "https://example.test/\n", "https://example.test:bad", "file:///x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                providers.validate_url(url)
        self.assertEqual(providers.validate_url("http://[::1]:9000/v1/"), "http://[::1]:9000/v1")

    def test_invalid_fields_and_model_lists_never_write(self):
        for patch in ({"weight": -1}, {"weight": True}, {"weight": 1000001}, {"models": []},
                      {"models": ["same", "same"]}, {"models": ["model with space"]},
                      {"disabled": "true"}, {"name": "first", "headers": {"Authorization": "bad"}}):
            backend = Management()
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                providers.upsert_provider(backend, {"name": "first", **patch}, weights_supported=True)
            self.assertTrue(all(method == "GET" for _, method, _ in backend.calls))

    def test_weights_rejected_on_unknown_backend(self):
        backend = Management()
        with self.assertRaisesRegex(ValueError, "unavailable"):
            providers.upsert_provider(backend, {"name": "first", "weight": 2})
        self.assertTrue(all(method == "GET" for _, method, _ in backend.calls))

    def test_explicit_test_makes_one_models_get_without_inference(self):
        backend = Management()
        backend.entries[0].pop("headers")
        request = Mock(return_value={"data": [{"id": "model-a", "secret": "bad-metadata"}]})
        result = providers.test_provider(backend, "first", request)
        request.assert_called_once_with("https://example.test/v1/models", key="fake-secret", method="GET", timeout=8)
        self.assertEqual(result["provider_test"]["models"], ["model-a"])
        self.assertFalse(result["provider_test"]["inference_tested"])
        self.assertNotIn("secret", str(result))

    def test_custom_headers_are_not_forwarded_by_test(self):
        request = Mock()
        with self.assertRaisesRegex(ValueError, "custom headers"):
            providers.test_provider(Management(), "first", request)
        request.assert_not_called()

    def test_test_error_body_or_key_is_never_returned(self):
        backend = Management()
        backend.entries[0].pop("headers")
        error = urllib.error.HTTPError("https://example.test", 401, "fake-secret", {}, io.BytesIO(b"private body"))
        with self.assertRaisesRegex(ValueError, "HTTP 401") as raised:
            providers.test_provider(backend, "first", Mock(side_effect=error))
        self.assertNotIn("secret", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    def test_credential_readback_mismatch_does_not_claim_success(self):
        backend = Management()
        def api(route, method="GET", body=None):
            return backend(route) if method == "PATCH" else backend(route, method, body)
        with self.assertRaisesRegex(ValueError, "credential"):
            providers.upsert_provider(api, {"name": "first", "weight": 0}, weights_supported=True)

    def test_readback_mismatch_does_not_claim_success(self):
        backend = Management()
        def api(route, method="GET", body=None):
            return backend(route) if method == "PATCH" else backend(route, method, body)
        with self.assertRaisesRegex(ValueError, "confirm"):
            providers.upsert_provider(api, {"name": "first", "url": "https://changed.test/v1"})


@unittest.skipUnless(os.environ.get("OMAPROXY_TEST_BINARY"), "set OMAPROXY_TEST_BINARY for isolated backend tests")
class ProviderBackendTests(unittest.TestCase):
    def test_real_backend_crud_retains_secret_and_unrelated_provider(self):
        from test_routing import isolated_backend
        with isolated_backend() as (api, config):
            providers.upsert_provider(api, {"name": "added", "url": "http://127.0.0.1:2/v1",
                                           "key": "fake-added-key", "models": "added-model"})
            providers.upsert_provider(api, {"name": "mock", "key": "", "url": "http://127.0.0.1:3/v1", "disabled": False})
            raw = api(providers.ROUTE)["openai-compatibility"]
            mock = next(entry for entry in raw if entry["name"] == "mock")
            self.assertEqual(mock["api-key-entries"][0]["api-key"], "fake-upstream-key")
            providers.upsert_provider(api, {"name": "mock", "weight": 0}, weights_supported=True)
            raw = api(providers.ROUTE)["openai-compatibility"]
            mock = next(entry for entry in raw if entry["name"] == "mock")
            self.assertEqual(mock["api-key-entries"][0]["weight"], 0)
            providers.remove_provider(api, "added")
            self.assertEqual([entry["name"] for entry in api(providers.ROUTE)["openai-compatibility"]], ["mock"])
            self.assertNotIn("fake-upstream-key", json.dumps(providers.list_providers(api)))
            self.assertIn("fixture comment must survive", config.read_text())


if __name__ == "__main__":
    unittest.main()
