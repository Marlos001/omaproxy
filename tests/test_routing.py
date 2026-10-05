"""Routing requests must preserve credentials and unrelated config fields."""
import copy
import contextlib
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
import io
from pathlib import Path
import sys
import unittest
import urllib.error

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import routing


class Management:
    def __init__(self, v8=False):
        self.v8, self.calls = v8, []
        self.config = {"api-keys": ["fake-client-secret"], "opaque": {"keep": True},
                       "routing": {"strategy": "round-robin", "session-affinity": False,
                                   "session-affinity-ttl": "1h", "session-affinity-subagents": True,
                                   "opaque": "keep"},
                       "request-retry": 3, "max-retry-credentials": 0, "max-retry-interval": 30}
        if v8:
            self.config["routing"]["retry"] = {field: self.config[field] for field in routing.SCALARS}
            self.config["routing"]["cooldown"] = {"disable-cooling": False, "save-cooldown-status": True}

    def __call__(self, route, method="GET", body=None):
        self.calls.append((route, method, copy.deepcopy(body)))
        if route == routing.V8_ROUTING:
            if not self.v8:
                raise urllib.error.HTTPError(route, 404, "missing", {}, io.BytesIO())
            if method == "PATCH":
                for field, value in body.items():
                    if isinstance(value, dict):
                        self.config["routing"].setdefault(field, {}).update(value)
                    else:
                        self.config["routing"][field] = value
                return {"status": "ok"}
            return copy.deepcopy(self.config["routing"])
        if route.startswith("/v8/"):
            raise urllib.error.HTTPError(route, 404, "missing", {}, io.BytesIO())
        if route == routing.V0 + "config":
            return copy.deepcopy(self.config)
        field = route.removeprefix(routing.V0)
        if field == "routing/strategy":
            if method == "PATCH":
                self.config["routing"]["strategy"] = body["value"]
            return {"strategy": self.config["routing"]["strategy"]}
        if field in routing.SCALARS:
            if method == "PATCH":
                self.config[field] = body["value"]
            return {field: self.config[field]}
        raise AssertionError(route)


class RoutingTests(unittest.TestCase):
    def test_v7_discovery_is_get_only_and_config_is_allowlisted(self):
        backend = Management()
        result = routing.read_settings(backend, "v7.2.154")
        self.assertTrue(result["weights"])
        self.assertFalse(result["capabilities"]["session-affinity"])
        self.assertNotIn("fake-client-secret", str(result))
        self.assertNotIn("opaque", str(result))
        self.assertTrue(all(method == "GET" for _, method, _ in backend.calls))

    def test_v7_omitted_affinity_fields_show_backend_defaults_read_only(self):
        backend = Management()
        for field in routing.AFFINITY:
            backend.config["routing"].pop(field)
        result = routing.read_settings(backend, "v7.2.154")
        self.assertFalse(result["values"]["session-affinity"])
        self.assertEqual(result["values"]["session-affinity-ttl"], "1h")
        self.assertTrue(result["values"]["session-affinity-subagents"])
        self.assertFalse(result["capabilities"]["session-affinity"])

    def test_unknown_and_old_backends_do_not_advertise_weighted(self):
        for version in (None, "custom", "v7.2.153"):
            with self.subTest(version=version):
                self.assertNotIn("weighted-round-robin", routing.read_settings(Management(), version)["strategies"])

    def test_weighted_current_strategy_is_evidence_on_custom_backend(self):
        backend = Management()
        backend.config["routing"]["strategy"] = "weighted-round-robin"
        self.assertTrue(routing.read_settings(backend)["weights"])

    def test_v7_scalar_changes_preserve_unrelated_settings(self):
        backend = Management()
        routing.update_settings(backend, {"strategy": "weighted-round-robin", "request-retry": 2}, "v7.2.154")
        self.assertEqual(backend.config["api-keys"], ["fake-client-secret"])
        self.assertEqual(backend.config["routing"]["opaque"], "keep")
        self.assertEqual([route for route, method, _ in backend.calls if method == "PATCH"],
                         [routing.V0 + "routing/strategy", routing.V0 + "request-retry"])

    def test_v7_unsupported_field_aborts_before_any_write(self):
        backend = Management()
        with self.assertRaisesRegex(ValueError, "safely edit"):
            routing.update_settings(backend, {"request-retry": 2, "session-affinity": True}, "v7.2.154")
        self.assertTrue(all(method == "GET" for _, method, _ in backend.calls))

    def test_v8_edits_only_routing_with_one_merge_patch(self):
        backend = Management(True)
        changes = {"session-affinity": True, "session-affinity-ttl": "2h30m",
                   "session-affinity-subagents": False, "request-retry": 2,
                   "max-retry-credentials": 4, "max-retry-interval": 10, "disable-cooling": False}
        routing.update_settings(backend, changes, "v8.0.13")
        writes = [(route, body) for route, method, body in backend.calls if method != "GET"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][0], routing.V8_ROUTING)
        self.assertEqual(writes[0][1]["retry"]["request-retry"], 2)
        self.assertEqual(backend.config["opaque"], {"keep": True})
        self.assertTrue(backend.config["routing"]["cooldown"]["save-cooldown-status"])

    def test_invalid_values_never_call_backend(self):
        for changes in ({}, {"api-keys": []}, {"request-retry": True}, {"request-retry": -1},
                        {"request-retry": 11}, {"max-retry-credentials": 101},
                        {"max-retry-interval": 301}, {"session-affinity": "true"},
                        {"session-affinity-ttl": "0s"}, {"session-affinity-ttl": "25h"},
                        {"session-affinity-ttl": "NaNh"}, {"strategy": "priority"}):
            with self.subTest(changes=changes):
                backend = Management()
                with self.assertRaises(ValueError):
                    routing.update_settings(backend, changes)
                self.assertFalse(backend.calls)

    def test_readback_mismatch_does_not_claim_success(self):
        backend = Management()
        def api(route, method="GET", body=None):
            return backend(route) if method == "PATCH" else backend(route, method, body)
        with self.assertRaisesRegex(ValueError, "confirm"):
            routing.update_settings(api, {"request-retry": 2}, "v7.2.154")

    def test_v7_partial_write_reports_applied_fields_without_error_body(self):
        backend = Management()
        def api(route, method="GET", body=None):
            if method == "PATCH" and route.endswith("request-retry"):
                raise ValueError("fake-secret-response")
            return backend(route, method, body)
        with self.assertRaisesRegex(ValueError, "already applied: strategy") as error:
            routing.update_settings(api, {"strategy": "fill-first", "request-retry": 2}, "v7.2.154")
        self.assertNotIn("fake-secret", str(error.exception))

    def test_auth_failure_is_not_misreported_as_missing_capability(self):
        def api(*args):
            raise urllib.error.HTTPError("local", 401, "unauthorized", {}, io.BytesIO())
        with self.assertRaises(urllib.error.HTTPError) as error:
            routing.read_settings(api)
        error.exception.close()


@contextlib.contextmanager
def isolated_backend():
    """Temporary config, separate listener, no live service/config mutations."""
    with tempfile.TemporaryDirectory() as temp:
        with contextlib.closing(socket.socket()) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        config = Path(temp) / "config.yaml"
        config.write_text("# fixture comment must survive\n" +
            f"host: 127.0.0.1\nport: {port}\nauth-dir: {temp}/auth\n" +
            "api-keys: [fake-client-key]\n" +
            "remote-management: {secret-key: fake-management-key, allow-remote: false, disable-control-panel: true}\n" +
            "request-retry: 3 # retry comment\nmax-retry-credentials: 2\nmax-retry-interval: 30\n" +
            "routing: {strategy: round-robin, session-affinity: false, session-affinity-ttl: 1h}\n" +
            "ws-auth: true # unrelated comment\n" +
            'openai-compatibility: [{name: mock, base-url: "http://127.0.0.1:1/v1", api-key-entries: [{api-key: fake-upstream-key, weight: 4}], models: [{name: mock-model, alias: mock-alias}]}]\n')
        process = subprocess.Popen([os.environ["OMAPROXY_TEST_BINARY"], "--config", str(config), "--local-model"],
                                   cwd=temp, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def api(route, method="GET", body=None):
            request = urllib.request.Request(f"http://127.0.0.1:{port}" + route,
                method=method, data=None if body is None else json.dumps(body).encode(),
                headers={"Authorization": "Bearer fake-management-key", "Content-Type": "application/json"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=4) as response:
                return json.load(response)
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("Isolated backend exited.")
                try:
                    api(routing.V0 + "routing/strategy")
                    break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.1)
            else:
                raise RuntimeError("Isolated backend did not become ready.")
            yield api, config
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


@unittest.skipUnless(os.environ.get("OMAPROXY_TEST_BINARY"), "set OMAPROXY_TEST_BINARY for isolated backend tests")
class RoutingBackendTests(unittest.TestCase):
    def test_real_backend_strategy_retry_and_comment_preservation(self):
        with isolated_backend() as (api, config):
            discovered = routing.read_settings(api, "v7.2.154")
            changes = {"strategy": "weighted-round-robin", "request-retry": 2, "max-retry-interval": 12}
            if discovered["v8_config"]:
                changes.update({"session-affinity": True, "session-affinity-ttl": "30m",
                                "session-affinity-subagents": False})
            routing.update_settings(api, changes, "v7.2.154")
            self.assertTrue(api(routing.V0 + "ws-auth")["ws-auth"])
            saved = config.read_text()
            for comment in ("fixture comment must survive", "retry comment", "unrelated comment"):
                self.assertIn(comment, saved)
            self.assertIn("fake-upstream-key", saved)


if __name__ == "__main__":
    unittest.main()
