import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("controls_bridge", SCRIPTS / "omaproxy.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

import client_keys
import quota_alerts


class ControlsBridgeTests(unittest.TestCase):
    def test_pending_update_blocks_mutations_before_network_io(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(bridge, "DATA", Path(temporary)), \
                patch.object(bridge, "settings", return_value={"port": 18317, "management_key": "fake"}), \
                patch.object(bridge, "request") as request:
            (Path(temporary) / "backend-pending").mkdir()
            with self.assertRaisesRegex(ValueError, "needs recovery"):
                bridge.api("api-keys", "DELETE")
            request.assert_not_called()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="omaproxy-controls-")
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "config/omaproxy"
        self.config_patch = patch.object(bridge, "CONFIG", self.config)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    def configure(self):
        settings = {
            "port": 18317,
            "management_key": "management-test-secret",
            "api_key": "protected-primary-test-key",
            "providers": [],
            "version": "test",
            "binary": "/fake/backend",
        }
        bridge.private_write(self.config / "settings.json", json.dumps(settings))
        return settings

    def test_quotas_cli_only_processes_alerts_after_opt_in_and_cached_write(self):
        self.configure()
        api = Mock(return_value={"files": []})
        output = io.StringIO()

        def process_after_write(snapshot, config_path):
            cached = json.loads((self.config / "quotas.json").read_text())
            self.assertEqual(snapshot, cached)
            self.assertEqual(config_path, self.config)
            return {"alert_count": 0}

        with patch.object(bridge, "api", api), \
             patch.object(quota_alerts, "process", side_effect=process_after_write) as process, \
             patch.object(sys, "argv", ["omaproxy.py", "quotas"]), \
             contextlib.redirect_stdout(output):
            bridge.main()

        default_result = json.loads(output.getvalue())
        self.assertIn("quotas", default_result)
        self.assertNotIn("alerts", default_result)
        process.assert_not_called()
        self.assertFalse((self.config / "quota-alerts.json").exists())

        output = io.StringIO()
        with patch.object(bridge, "api", api), \
             patch.object(quota_alerts, "process", side_effect=process_after_write) as process, \
             patch.object(sys, "argv", ["omaproxy.py", "quotas", "--notify"]), \
             contextlib.redirect_stdout(output):
            bridge.main()

        opted_in_result = json.loads(output.getvalue())
        self.assertEqual(opted_in_result["alerts"], {"alert_count": 0})
        process.assert_called_once()
        api.assert_called_with("auth-files", cfg=bridge.settings())

    def test_client_revoke_passes_primary_key_from_private_settings(self):
        settings = self.configure()
        settings_path = self.config / "settings.json"
        settings_before = settings_path.read_text()
        revoke = Mock(return_value={"revoked": True, "name": "downstream"})
        output = io.StringIO()

        with patch.object(client_keys, "revoke_key", revoke), \
             patch.object(sys, "argv", ["omaproxy.py", "client-revoke", "downstream"]), \
             contextlib.redirect_stdout(output):
            bridge.main()

        self.assertEqual(revoke.call_count, 1)
        self.assertIs(revoke.call_args.args[0], bridge.api)
        self.assertEqual(revoke.call_args.args[1], self.config / "client-keys.json")
        self.assertEqual(revoke.call_args.args[2], settings["management_key"])
        self.assertEqual(revoke.call_args.args[3], "downstream")
        self.assertEqual(revoke.call_args.kwargs["primary_key"], settings["api_key"])
        self.assertEqual(settings_path.read_text(), settings_before)
        self.assertNotIn(settings["api_key"], output.getvalue())
        self.assertNotIn(settings["management_key"], output.getvalue())


if __name__ == "__main__":
    unittest.main()
