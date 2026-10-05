"""Optional real Codex clients, with an isolated home and fake loopback provider.

Set both OMAPROXY_TEST_BINARY and OMAPROXY_TEST_CODEX_BINARY. No installed user
configuration, credentials, MCP servers, hooks, or transcripts are adopted.
"""
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import unittest

from responses_fixture import CLIENT_KEY, ResponsesBackend, TEXT


class AppServer:
    def __init__(self, binary, cwd, env):
        self.messages = queue.Queue()
        self.stderr = tempfile.TemporaryFile(mode="w+")
        try:
            self.process = subprocess.Popen([binary, "app-server", "--listen", "stdio://"],
                cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self.stderr, text=True)
        except BaseException:
            self.stderr.close()
            raise
        self.next_id = 1
        self.seen = []

        def read():
            try:
                for line in self.process.stdout:
                    self.messages.put(json.loads(line))
            except BaseException as error:
                self.messages.put(error)
            finally:
                self.messages.put(EOFError("app-server stdout closed"))

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdin.close()
        self.reader.join(timeout=1)
        self.process.stdout.close()
        self.stderr.close()

    def send(self, value):
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()

    def wait(self, predicate, timeout=15):
        for message in self.seen:
            if predicate(message):
                return message
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if isinstance(message, BaseException):
                raise message
            self.seen.append(message)
            if "id" in message and "method" in message:
                raise AssertionError("Unexpected server request in fixture: " + message["method"])
            if predicate(message):
                return message
        raise AssertionError("Timed out waiting for app-server; received methods: " +
                             repr([message.get("method") for message in self.seen]))

    def call(self, method, params):
        rid = self.next_id
        self.next_id += 1
        self.send({"id": rid, "method": method, "params": params})
        response = self.wait(lambda value: value.get("id") == rid)
        if "error" in response:
            raise AssertionError(f"{method}: {response['error']}")
        return response["result"]


@unittest.skipUnless(os.environ.get("OMAPROXY_TEST_BINARY") and os.environ.get("OMAPROXY_TEST_CODEX_BINARY"),
                     "set OMAPROXY_TEST_BINARY and OMAPROXY_TEST_CODEX_BINARY for real Codex clients")
class CodexClientsIntegration(unittest.TestCase):
    def setUp(self):
        self.backend = self.enterContext(ResponsesBackend())
        self.temp = self.enterContext(tempfile.TemporaryDirectory(prefix="omaproxy-codex-test-"))
        codex_home = Path(self.temp) / "codex-home"
        codex_home.mkdir()
        self.cwd = Path(self.temp) / "workspace"
        self.cwd.mkdir()
        # Deliberately allowlist child environment instead of inheriting provider keys.
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.temp,
            "CODEX_HOME": str(codex_home), "XDG_CONFIG_HOME": self.temp,
            "XDG_DATA_HOME": self.temp, "LANG": "C.UTF-8", "OMAPROXY_FIXTURE_KEY": CLIENT_KEY}
        (codex_home / "config.toml").write_text(f'''model_provider = "fixture"
model = "test-model"
approval_policy = "never"
sandbox_mode = "read-only"
model_reasoning_effort = "low"
[model_providers.fixture]
name = "Loopback test fixture"
base_url = "{self.backend.base}/v1"
env_key = "OMAPROXY_FIXTURE_KEY"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
stream_idle_timeout_ms = 8000
[analytics]
enabled = false
[feedback]
enabled = false
[features]
shell_snapshot = false
''')
        self.binary = os.environ["OMAPROXY_TEST_CODEX_BINARY"]
        version = subprocess.run([self.binary, "--version"], cwd=self.cwd, env=self.env,
            capture_output=True, text=True, timeout=5, check=True)
        self.assertIn("codex", version.stdout.lower())

    def test_codex_exec_completes_with_fake_provider(self):
        process = subprocess.run([self.binary, "exec", "--ephemeral", "--skip-git-repo-check",
            "--sandbox", "read-only", "--json", "fixture:text"], cwd=self.cwd, env=self.env,
            capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        events = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
        self.assertTrue(any(event["type"] == "turn.completed" for event in events), process.stdout)
        messages = [event["item"] for event in events if event["type"] == "item.completed"
                    and event["item"]["type"] == "agent_message"]
        self.assertEqual([message["text"] for message in messages], [TEXT])
        self.assertTrue(self.backend.received, "Codex never reached the isolated fake upstream")
        self.assertTrue(all(path == "/primary/v1/chat/completions" for path, _, _ in self.backend.received))
        self.assertFalse(any(event["type"] == "turn.failed" for event in events))

    def start_app_server(self):
        server = AppServer(self.binary, self.cwd, self.env)
        self.addCleanup(server.close)
        initialized = server.call("initialize", {"clientInfo": {
            "name": "omaproxy_protocol_test", "title": "OmaProxy protocol test", "version": "1.0.0"}})
        self.assertIn("userAgent", initialized)
        server.send({"method": "initialized", "params": {}})
        thread = server.call("thread/start", {"model": "test-model", "modelProvider": "fixture",
            "cwd": str(self.cwd), "approvalPolicy": "never", "sandbox": "read-only", "ephemeral": True})
        return server, thread["thread"]["id"]

    def test_app_server_initializes_and_completes_a_turn(self):
        server, thread_id = self.start_app_server()
        result = server.call("turn/start", {"threadId": thread_id,
            "input": [{"type": "text", "text": "fixture:text"}]})
        turn_id = result["turn"]["id"]
        terminal = server.wait(lambda value: value.get("method") == "turn/completed"
                               and value["params"]["turn"]["id"] == turn_id)
        self.assertEqual(terminal["params"]["threadId"], thread_id)
        self.assertEqual(terminal["params"]["turn"]["status"], "completed")
        deltas = [message["params"]["delta"] for message in server.seen
                  if message.get("method") == "item/agentMessage/delta"]
        self.assertEqual("".join(deltas), TEXT)
        items = [message["params"]["item"] for message in server.seen
                 if message.get("method") == "item/completed"
                 and message["params"]["item"]["type"] == "agentMessage"]
        self.assertEqual([item["text"] for item in items], [TEXT])
        self.assertTrue(self.backend.received, "app-server never reached the isolated fake upstream")

    def test_app_server_interrupt_cancels_inference_and_next_turn_works(self):
        server, thread_id = self.start_app_server()
        result = server.call("turn/start", {"threadId": thread_id,
            "input": [{"type": "text", "text": "fixture:hold"}]})
        turn_id = result["turn"]["id"]
        server.wait(lambda value: value.get("method") == "item/agentMessage/delta")
        self.assertTrue(self.backend.hold_started.wait(1))
        server.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        terminal = server.wait(lambda value: value.get("method") == "turn/completed"
                               and value["params"]["turn"]["id"] == turn_id)
        self.assertEqual(terminal["params"]["turn"]["status"], "interrupted")
        self.assertTrue(self.backend.cancelled.wait(3), "app-server interrupt did not cancel upstream")
        result = server.call("turn/start", {"threadId": thread_id,
            "input": [{"type": "text", "text": "fixture:text"}]})
        next_id = result["turn"]["id"]
        terminal = server.wait(lambda value: value.get("method") == "turn/completed"
                               and value["params"]["turn"]["id"] == next_id)
        self.assertEqual(terminal["params"]["turn"]["status"], "completed")
        items = [message["params"]["item"] for message in server.seen
                 if message.get("method") == "item/completed"
                 and message["params"].get("turnId") == next_id
                 and message["params"]["item"]["type"] == "agentMessage"]
        self.assertEqual([item["text"] for item in items], [TEXT])


if __name__ == "__main__":
    unittest.main()
