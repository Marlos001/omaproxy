import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import quota_alerts


NOW = 1_800_000_000


def snapshot(*, remaining=8, reset_at=NOW + 3600, checked_at=NOW,
             updated_at=NOW, status="ok", stale=False, error="", disabled=False,
             label="Codex Spark · 5-hour", provider="codex", auth_index="private-account-id",
             email="person@example.com", windows=None):
    return {"checked_at": checked_at, "accounts": [{
        "name": "person-auth.json", "auth_index": auth_index, "email": email,
        "provider": provider, "status": status, "checked_at": checked_at,
        "updated_at": updated_at, "stale": stale, "error": error,
        "disabled": disabled,
        "windows": windows if windows is not None else [{
            "label": label, "remaining_percent": remaining, "reset_at": reset_at,
        }],
    }]}


class QuotaAlertTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="omaproxy-alerts-")
        self.config = Path(self.temp.name) / "config"
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def runner(self, returncode=0):
        def call(argv, **kwargs):
            self.calls.append((argv, kwargs))
            return SimpleNamespace(returncode=returncode)
        return call

    def notify_send(self):
        return patch.object(quota_alerts.shutil, "which", return_value="/usr/bin/notify-send")

    def test_low_quota_uses_generic_safe_labels_and_private_state(self):
        with self.notify_send(), patch.dict(os.environ, {"QUOTA_ALERT_TEST_SECRET": "do-not-forward"}):
            result = quota_alerts.process(snapshot(), self.config, NOW, self.runner())

        self.assertEqual(result, {"alert_count": 1})
        argv, kwargs = self.calls[0]
        self.assertEqual(argv[0], "/usr/bin/notify-send")
        self.assertIn("Codex · 5-hour: 8% remaining.", argv[-1])
        joined = json.dumps(argv)
        self.assertNotIn("person@example.com", joined)
        self.assertNotIn("person-auth.json", joined)
        self.assertNotIn("private-account-id", joined)
        self.assertEqual(kwargs["timeout"], 5)
        self.assertIs(kwargs["shell"], False)
        self.assertNotIn("QUOTA_ALERT_TEST_SECRET", kwargs["env"])
        state_path = self.config / "quota-alerts.json"
        self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)
        state = json.loads(state_path.read_text())
        self.assertLessEqual(len(state["entries"]), quota_alerts.MAX_STATE_ENTRIES)
        self.assertNotIn("private-account-id", state_path.read_text())
        self.assertNotIn("person@example.com", state_path.read_text())
        self.assertNotIn("Spark", state_path.read_text())

    def test_same_low_quota_window_is_sent_once(self):
        with self.notify_send():
            first = quota_alerts.process(snapshot(), self.config, NOW, self.runner())
            second_snapshot = snapshot(checked_at=NOW + 30, updated_at=NOW + 30)
            second = quota_alerts.process(second_snapshot, self.config, NOW + 30, self.runner())

        self.assertEqual(first["alert_count"], 1)
        self.assertEqual(second["alert_count"], 0)
        self.assertEqual(len(self.calls), 1)

    def test_reset_notice_requires_observed_reset_change_and_more_allowance(self):
        with self.notify_send():
            initial = quota_alerts.process(
                snapshot(remaining=30, reset_at=NOW + 3600), self.config, NOW, self.runner())
            changed = quota_alerts.process(
                snapshot(remaining=70, reset_at=NOW + 7200, checked_at=NOW + 60,
                         updated_at=NOW + 60), self.config, NOW + 60, self.runner())

        self.assertEqual(initial["alert_count"], 0)
        self.assertEqual(changed["alert_count"], 1)
        self.assertIn("allowance refreshed", self.calls[0][0][-1])

    def test_timer_or_allowance_increase_without_reset_change_does_not_alert_reset(self):
        with self.notify_send():
            quota_alerts.process(snapshot(remaining=30), self.config, NOW, self.runner())
            changed = quota_alerts.process(
                snapshot(remaining=80, checked_at=NOW + 60, updated_at=NOW + 60),
                self.config, NOW + 60, self.runner())

        self.assertEqual(changed["alert_count"], 0)
        self.assertEqual(self.calls, [])

    def test_unknown_previous_reset_does_not_prove_a_reset_transition(self):
        with self.notify_send():
            quota_alerts.process(snapshot(remaining=30, reset_at=None),
                                 self.config, NOW, self.runner())
            changed = quota_alerts.process(
                snapshot(remaining=80, reset_at=NOW + 7200,
                         checked_at=NOW + 60, updated_at=NOW + 60),
                self.config, NOW + 60, self.runner())

        self.assertEqual(changed["alert_count"], 0)
        self.assertEqual(self.calls, [])

    def test_stale_old_error_disabled_unknown_and_nonfinite_readings_are_ignored(self):
        invalid_snapshots = [
            snapshot(checked_at=NOW - quota_alerts.MAX_DATA_AGE_SECONDS - 1,
                     updated_at=NOW - quota_alerts.MAX_DATA_AGE_SECONDS - 1),
            snapshot(stale=True),
            snapshot(error="quota request failed"),
            snapshot(disabled=True),
            snapshot(remaining=None),
            snapshot(remaining="NaN"),
            snapshot(remaining="Infinity"),
            snapshot(remaining=True),
            snapshot(label="untrusted account@example.com"),
        ]
        results = []
        with self.notify_send():
            for data in invalid_snapshots:
                results.append(quota_alerts.process(data, self.config, NOW, self.runner()))

        self.assertTrue(all(result["alert_count"] == 0 for result in results))
        self.assertEqual(self.calls, [])

    def test_quota_network_error_does_not_become_an_authentication_alert(self):
        data = snapshot(status="ok", error="401 sign in again to refresh quota")
        with self.notify_send():
            result = quota_alerts.process(data, self.config, NOW, self.runner())
        self.assertEqual(result["alert_count"], 0)
        self.assertEqual(self.calls, [])

    def test_only_explicit_health_status_emits_generic_auth_alert(self):
        with self.notify_send():
            ignored = quota_alerts.process(snapshot(status="error", remaining=50),
                                           self.config, NOW, self.runner())
            also_ignored = quota_alerts.process(snapshot(status="failed", remaining=50,
                                                        checked_at=NOW + 1, updated_at=NOW + 1),
                                                self.config, NOW + 1, self.runner())
            failed = quota_alerts.process(snapshot(status="auth_error", remaining=50,
                                                   checked_at=NOW + 1, updated_at=NOW + 1),
                                          self.config, NOW + 1, self.runner())

        self.assertEqual(ignored["alert_count"], 0)
        self.assertEqual(also_ignored["alert_count"], 0)
        self.assertEqual(failed["alert_count"], 1)
        self.assertIn("Codex account needs sign-in.", self.calls[0][0][-1])
        self.assertNotIn("person@example.com", json.dumps(self.calls[0][0]))

    def test_failed_delivery_retries_and_success_is_marked_sent(self):
        with self.notify_send():
            failed = quota_alerts.process(snapshot(), self.config, NOW, self.runner(1))
            retried = quota_alerts.process(
                snapshot(checked_at=NOW + 30, updated_at=NOW + 30),
                self.config, NOW + 30, self.runner(0))
            duplicate = quota_alerts.process(
                snapshot(checked_at=NOW + 60, updated_at=NOW + 60),
                self.config, NOW + 60, self.runner(0))

        self.assertEqual(failed["alert_count"], 0)
        self.assertIn("could not be delivered", failed["error"])
        self.assertEqual(retried["alert_count"], 1)
        self.assertEqual(duplicate["alert_count"], 0)
        self.assertEqual(len(self.calls), 2)

    def test_missing_notify_send_reports_unsupported_once_and_keeps_pending(self):
        with patch.object(quota_alerts.shutil, "which", return_value=None):
            first = quota_alerts.process(snapshot(), self.config, NOW, self.runner())
            second = quota_alerts.process(
                snapshot(checked_at=NOW + 30, updated_at=NOW + 30),
                self.config, NOW + 30, self.runner())

        self.assertIn("notify-send not found", first["error"])
        self.assertNotIn("error", second)
        self.assertEqual(self.calls, [])
        pending = [entry for entry in json.loads((self.config / "quota-alerts.json").read_text())["entries"].values()
                   if entry.get("delivery") == "pending"]
        self.assertEqual(len(pending), 1)

    def test_state_is_bounded_to_two_hundred_entries(self):
        data = {"checked_at": NOW, "accounts": []}
        for index in range(MAX_STATE_ENTRIES := quota_alerts.MAX_STATE_ENTRIES + 5):
            row = snapshot(remaining=50, auth_index=f"account-{index}")["accounts"][0]
            data["accounts"].append(row)
        with self.notify_send():
            quota_alerts.process(data, self.config, NOW, self.runner())
        stored = json.loads((self.config / "quota-alerts.json").read_text())
        self.assertLessEqual(len(stored["entries"]), MAX_STATE_ENTRIES)

    def test_stale_top_level_snapshot_does_not_create_or_update_state(self):
        result = quota_alerts.process(
            snapshot(checked_at=NOW - quota_alerts.MAX_DATA_AGE_SECONDS - 1),
            self.config, NOW, self.runner())
        self.assertEqual(result, {"alert_count": 0})
        self.assertFalse(self.config.exists())


if __name__ == "__main__":
    unittest.main()
