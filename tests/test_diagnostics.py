import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import diagnostics


def counter(success=2, failed=1):
    return {"success": success, "failed": failed,
            "recent_requests": [{"time": "09:00-09:10", "success": success, "failed": failed}]}


def missing():
    return HTTPError("http://private.invalid/secret", 404, "private key", {}, None)


class DiagnosticsTests(unittest.TestCase):
    def test_v8_uses_exact_schema_and_upstream_scope(self):
        raw = {"codex": {"https://user:private@example.invalid|sk-private": counter()}}
        api = Mock(return_value=raw)
        result = diagnostics.snapshot(api, accounts=[], salt="private-salt")
        self.assertEqual(api.call_args.args[0], "/v8/management/observability/usage/api-keys")
        self.assertEqual(api.call_args.kwargs, {"timeout": 2})
        self.assertEqual(result["usage"]["availability"], "available")
        record = result["usage"]["records"][0]
        self.assertEqual(record["success"], 2)
        self.assertTrue(record["label"].startswith("upstream-key-"))
        self.assertEqual(result["client_attribution"], "unavailable")
        self.assertEqual(result["queue"]["events"], [])
        self.assertNotIn("private", json.dumps(result))
        self.assertIn("exclude OAuth", " ".join(result["limitations"]))

    def test_v7_fallback_and_oauth_account_counters(self):
        api = Mock(side_effect=[missing(), {}, missing(), {"files": [dict(counter(),
             provider="codex", auth_index="auth-private", id="email@example.invalid.json")]}])
        result = diagnostics.snapshot(api, salt=b"installation-salt")
        self.assertEqual([call.args[0] for call in api.call_args_list], [
            "/v8/management/observability/usage/api-keys", "/v0/management/api-key-usage",
            "/v8/management/credentials", "/v0/management/auth-files"])
        self.assertEqual(result["accounts"]["records"][0]["success"], 2)
        self.assertEqual(result["usage"]["records"], [])
        self.assertNotIn("email", json.dumps(result))
        self.assertNotIn("auth-private", json.dumps(result))

    def test_never_probes_nonexistent_usage_or_consumes_queue_by_default(self):
        api = Mock(side_effect=[missing(), missing()])
        result = diagnostics.snapshot(api, accounts=[])
        self.assertEqual(result["usage"]["availability"], "unsupported")
        self.assertEqual(api.call_count, 2)
        for call in api.call_args_list:
            self.assertNotIn("queue", call.args[0])
            self.assertNotEqual(call.args[0], "/v0/management/usage")

    def test_auth_failure_does_not_fallback_and_never_echoes_exception(self):
        for code in (401, 403, 429, 500):
            api = Mock(side_effect=HTTPError("http://email@example.invalid/sk-secret", code,
                                            "Bearer sk-secret", {}, None))
            result = diagnostics.snapshot(api, accounts=[])
            self.assertEqual(api.call_count, 1)
            self.assertEqual(result["usage"]["availability"], "unavailable")
            self.assertNotIn("secret", json.dumps(result))
            self.assertNotIn("email", json.dumps(result))
        result = diagnostics.snapshot(Mock(side_effect=RuntimeError("sk-secret")), accounts=[])
        self.assertNotIn("sk-secret", json.dumps(result))

    def test_empty_is_valid_but_invalid_is_unknown(self):
        valid = diagnostics.snapshot(Mock(return_value={}), accounts=[])
        self.assertEqual(valid["usage"]["availability"], "available")
        self.assertEqual(valid["usage"]["retained"], 0)
        for raw in (None, [], {"error": "private"}, {"codex": {"key": {}}}):
            result = diagnostics.snapshot(Mock(return_value=raw), accounts=[])
            self.assertEqual(result["usage"]["availability"], "unknown")
            self.assertNotIn("private", json.dumps(result))

    def test_malformed_counts_are_unknown_not_zero(self):
        for value in (None, True, -1, float("nan"), "2", 2**70):
            result = diagnostics.snapshot(Mock(return_value={"codex": {"key": counter(value)}}), accounts=[])
            self.assertEqual(result["usage"]["records"], [])
            self.assertEqual(result["usage"]["invalid"], 1)

    def test_labels_are_stable_with_private_salt_and_change_across_installations(self):
        api = Mock(return_value={"codex": {"key": counter()}})
        one = diagnostics.snapshot(api, accounts=[], salt="salt-one")
        two = diagnostics.snapshot(api, accounts=[], salt="salt-one")
        other = diagnostics.snapshot(api, accounts=[], salt="salt-two")
        self.assertEqual(one["usage"]["records"], two["usage"]["records"])
        self.assertNotEqual(one["usage"]["records"][0]["label"], other["usage"]["records"][0]["label"])

    def test_bounds_records_and_rejects_free_text_buckets_providers(self):
        rows = {"private-" + str(i): counter() for i in range(200)}
        result = diagnostics.snapshot(Mock(return_value={"sk-secret@example.invalid": rows}), accounts=[])
        self.assertEqual(result["usage"]["retained"], 128)
        self.assertEqual(result["usage"]["omitted"], 72)
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("secret", json.dumps(result))
        item = counter()
        item["recent_requests"] = [{"time": "sk-secret", "success": 0, "failed": 0}] * 100
        result = diagnostics.snapshot(Mock(return_value={"codex": {"key": item}}), accounts=[])
        self.assertEqual(len(result["usage"]["records"][0]["recent_requests"]), 20)
        self.assertNotIn("sk-secret", json.dumps(result))

    def test_explicit_queue_capture_bounded_and_attribution_only_receipt_fields(self):
        raw = {"api_key": "sk-client", "auth_index": "email@example.invalid.json",
               "request_id": "secret-request", "execution_id": "secret-execution", "model": "secret-model",
               "provider": "codex", "timestamp": "2026-10-03T12:10:00Z", "failed": True,
               "latency_ms": 110, "ttft_ms": 30, "tokens": {"input_tokens": 4, "output_tokens": 5},
               "fail": {"status_code": 429, "body": "private prompt"}, "prompt": "private prompt",
               "response_headers": {"Authorization": "Bearer sk-token"}, "tool": "private tools"}
        api = Mock(side_effect=[{}, [raw]])
        result = diagnostics.snapshot(api, accounts=[], salt="private-salt", consume_queue=True)
        self.assertEqual(api.call_args.args[0], "/v8/management/observability/usage/queue?count=50")
        self.assertTrue(result["queue"]["consumed"])
        event = result["queue"]["events"][0]
        self.assertEqual(event["outcome"], "failed")
        self.assertEqual(event["status_code"], 429)
        self.assertEqual(event["latency_ms"], 110)
        self.assertEqual(event["tokens"], {"input_tokens": 4, "output_tokens": 5})
        self.assertIn("client_label", event)
        self.assertIn("account_label", event)
        self.assertNotIn("retries", event)
        for secret in ("private", "sk-client", "email@", "secret-request", "secret-model", "Bearer", "Authorization"):
            self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(len(diagnostics.sanitize_events([raw] * 100)), 50)
        unidentified = diagnostics.sanitize_events([{"source": "email.json", "failed": False}])[0]
        self.assertNotIn("client_label", unidentified)
        self.assertNotIn("account_label", unidentified)
        self.assertNotIn("latency_ms", unidentified)
        self.assertNotIn("tokens", unidentified)

    def test_unrecognized_queue_schema_and_v7_capture_fallback(self):
        api = Mock(side_effect=[{}, missing(), [{"failed": False}]])
        result = diagnostics.snapshot(api, accounts=[], consume_queue=True)
        self.assertEqual(result["queue"]["source"], "/v0/management/usage-queue?count=50")
        self.assertEqual(result["queue"]["events"][0]["outcome"], "success")
        api = Mock(side_effect=[{}, {"api_key": "private"}])
        result = diagnostics.snapshot(api, accounts=[], consume_queue=True)
        self.assertEqual(result["queue"]["availability"], "unknown")
        self.assertNotIn("private", json.dumps(result))

    def test_capture_counts_invalid_and_omitted_in_distinct_populations(self):
        # The five oldest receipts are beyond the display limit. Only the newest
        # fifty records are inspected, including three invalid records.
        discarded = [{"api_key": "private-old-client", "failed": False}] * 5
        inspected = [None, "private-body", {"prompt": "private-prompt"}]
        inspected += [{"failed": False, "latency_ms": number} for number in range(47)]
        api = Mock(side_effect=[{}, discarded + inspected])
        result = diagnostics.snapshot(api, accounts=[], consume_queue=True)
        queue = result["queue"]
        self.assertEqual(queue["availability"], "available")
        self.assertTrue(queue["capture_requested"])
        self.assertTrue(queue["consumed"])
        self.assertEqual(queue["retained"], 47)
        self.assertEqual(queue["invalid"], 3)
        self.assertEqual(queue["omitted"], 5)
        self.assertEqual(queue["retained"] + queue["invalid"] + queue["omitted"], 55)
        self.assertEqual([event["latency_ms"] for event in queue["events"]], list(range(47)))
        self.assertEqual(result["client_attribution"], "unavailable")
        self.assertNotIn("private", json.dumps(result))

    def test_all_invalid_capture_is_consumed_but_schema_unknown(self):
        records = [None, {}, {"prompt": "private-prompt"}, {"source": "private-auth.json"},
                   {"failed": "false", "latency_ms": "10", "api_key": None}]
        api = Mock(side_effect=[{}, records])
        result = diagnostics.snapshot(api, accounts=[], consume_queue=True)
        queue = result["queue"]
        self.assertEqual(queue["availability"], "unknown")
        self.assertTrue(queue["consumed"])
        self.assertTrue(queue["capture_requested"])
        self.assertEqual(queue["events"], [])
        self.assertEqual(queue["retained"], 0)
        self.assertEqual(queue["invalid"], 5)
        self.assertEqual(queue["omitted"], 0)
        self.assertEqual(result["client_attribution"], "unavailable")
        self.assertIn("schema", queue["error"])
        self.assertNotIn("private", json.dumps(result))

    def test_empty_capture_is_available_without_attribution(self):
        api = Mock(side_effect=[{}, []])
        result = diagnostics.snapshot(api, accounts=[], consume_queue=True)
        queue = result["queue"]
        self.assertEqual(queue["availability"], "available")
        self.assertTrue(queue["consumed"])
        self.assertEqual(queue["events"], [])
        self.assertEqual((queue["retained"], queue["invalid"], queue["omitted"]), (0, 0, 0))
        self.assertEqual(result["client_attribution"], "unavailable")

    def test_receipt_presence_alone_does_not_establish_client_attribution(self):
        for key in (None, "", 123, True, "x" * 16385):
            with self.subTest(api_key_type=type(key).__name__):
                receipt = {"failed": False, "api_key": key, "auth_index": "private-account",
                           "request_id": "private-request", "source": "private-auth.json"}
                api = Mock(side_effect=[{}, [receipt]])
                result = diagnostics.snapshot(api, accounts=[], consume_queue=True)
                self.assertEqual(result["queue"]["availability"], "available")
                self.assertEqual(result["queue"]["retained"], 1)
                event = result["queue"]["events"][0]
                self.assertIn("account_label", event)
                self.assertIn("request_label", event)
                self.assertNotIn("client_label", event)
                self.assertEqual(result["client_attribution"], "unavailable")
                self.assertNotIn("private", json.dumps(result))
        api = Mock(side_effect=[{}, [{"failed": False}, {"failed": True, "api_key": "private-client"}]])
        result = diagnostics.snapshot(api, accounts=[], consume_queue=True)
        self.assertEqual(result["client_attribution"], "receipt_fields_only")
        self.assertNotIn("client_label", result["queue"]["events"][0])
        self.assertIn("client_label", result["queue"]["events"][1])
        self.assertNotIn("private-client", json.dumps(result))

    def test_denied_capture_was_requested_but_not_consumed(self):
        denied = HTTPError("http://private.invalid/private-key", 403, "private-error", {}, None)
        api = Mock(side_effect=[{}, denied])
        result = diagnostics.snapshot(api, accounts=[], consume_queue=True)
        self.assertEqual(api.call_count, 2)
        self.assertEqual(result["queue"]["availability"], "unavailable")
        self.assertTrue(result["queue"]["capture_requested"])
        self.assertFalse(result["queue"]["consumed"])
        self.assertEqual(result["queue"]["events"], [])
        self.assertEqual(result["client_attribution"], "unavailable")
        self.assertNotIn("private", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
