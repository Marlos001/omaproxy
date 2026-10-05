"""Deterministic Chat Completions upstream for real Responses bridge tests.

Only loopback, fake keys, and synthetic prompts are used. No client response is
mocked: the CLIProxyAPI process must translate these upstream Chat events.
"""
import contextlib
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request

CLIENT_KEY = "responses-test-client-key"
UPSTREAM_KEY = "responses-test-upstream-key"
TEXT = "Hello, coding client."
REASONING = "Check the fixture first."
ARGUMENTS = '{"path":"README.md"}'
USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
         "prompt_tokens_details": {"cached_tokens": 3},
         "completion_tokens_details": {"reasoning_tokens": 2}}


def scenario_for(body):
    # The newest user input selects the fixture when real clients replay a
    # transcript containing earlier fixture prompts after an interrupted turn.
    for message in reversed(body.get("messages", [])):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.startswith("fixture:"):
            return content.split(":", 1)[1]
        if isinstance(content, list):
            for part in content:
                text = part.get("text", "")
                if text.startswith("fixture:"):
                    return text.split(":", 1)[1]
    return "text"


class ResponsesBackend:
    def __init__(self):
        self.received = []
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.hold_started = threading.Event()
        self.release = threading.Event()

    def __enter__(self):
        self.stack = contextlib.ExitStack()
        self.stack.__enter__()
        try:
            temp = self.stack.enter_context(tempfile.TemporaryDirectory())
            fixture = self

            class Upstream(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def log_message(self, *args):
                    pass

                def do_POST(self):
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    with fixture.lock:
                        fixture.received.append((self.path, self.headers.get("Authorization"), body))
                    scenario = scenario_for(body)
                    if scenario == "retry" and self.path.startswith("/primary/"):
                        result = b'{"error":{"message":"fixture unavailable","type":"server_error"}}'
                        self.send_response(503)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(result)))
                        self.end_headers()
                        self.wfile.write(result)
                        return
                    rid = "chat_fixture_" + scenario
                    tools = scenario in ("tools", "parallel-tools") and not any(
                        msg.get("role") == "tool" for msg in body.get("messages", []))
                    if tools:
                        calls = [{"id": "call_read", "type": "function", "function": {
                            "name": "read_file", "arguments": ARGUMENTS}}]
                        if scenario == "parallel-tools":
                            calls.append({"id": "call_list", "type": "function", "function": {
                                "name": "list_files", "arguments": '{"directory":"."}'}})
                        message = {"role": "assistant", "content": None, "tool_calls": calls}
                    else:
                        text = '{"ok":true}' if scenario == "structured" else TEXT
                        if scenario.startswith("concurrent-"):
                            text = scenario
                        if scenario == "tools":
                            text = "Tool result received."
                        message = {"role": "assistant", "content": text}
                        if scenario == "reasoning":
                            message["reasoning_content"] = REASONING
                    if not body.get("stream"):
                        result = json.dumps({"id": rid, "object": "chat.completion", "created": 1700000000,
                            "model": body["model"], "choices": [{"index": 0, "message": message,
                            "finish_reason": "tool_calls" if tools else "stop"}], "usage": USAGE}).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(result)))
                        self.end_headers()
                        self.wfile.write(result)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True

                    def chunk(delta, finish=None, usage=None):
                        value = {"id": rid, "object": "chat.completion.chunk", "created": 1700000000,
                            "model": body["model"], "choices": [{"index": 0, "delta": delta,
                            "finish_reason": finish}]}
                        if usage:
                            value["usage"] = usage
                        self.wfile.write(b"data: " + json.dumps(value).encode() + b"\n\n")
                        self.wfile.flush()

                    try:
                        chunk({"role": "assistant"})
                        if tools:
                            for index, call in enumerate(message["tool_calls"]):
                                args = call["function"]["arguments"]
                                chunk({"tool_calls": [{"index": index, "id": call["id"], "type": "function",
                                    "function": {"name": call["function"]["name"], "arguments": args[:8]}}]})
                                chunk({"tool_calls": [{"index": index, "function": {"arguments": args[8:]}}]})
                        else:
                            if scenario == "reasoning":
                                chunk({"reasoning_content": REASONING[:10]})
                                chunk({"reasoning_content": REASONING[10:]})
                            text = message["content"]
                            chunk({"content": text[:7]})
                            if scenario == "interrupted":
                                self.connection.shutdown(socket.SHUT_RDWR)
                                return
                            if scenario == "hold":
                                fixture.hold_started.set()
                                # Repeated writes expose cancellation even when the Go process
                                # has buffered an upstream frame. Bounded to avoid leaked handlers.
                                for _ in range(300):
                                    if fixture.release.wait(0.02):
                                        break
                                    chunk({"content": "x" * 8192})
                                return
                            if scenario.startswith("concurrent-"):
                                time.sleep(0.05)
                            chunk({"content": text[7:]})
                        chunk({}, "tool_calls" if tools else "stop", USAGE)
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        if scenario == "hold":
                            fixture.cancelled.set()

            self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
            self.upstream.daemon_threads = True
            self.stack.callback(self.upstream.server_close)
            self.stack.callback(self.upstream.shutdown)
            self.stack.callback(self.release.set)
            threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
            with contextlib.closing(socket.socket()) as sock:
                sock.bind(("127.0.0.1", 0))
                self.port = sock.getsockname()[1]
            self.base = f"http://127.0.0.1:{self.port}"
            config = {"host": "127.0.0.1", "port": self.port, "auth-dir": str(Path(temp) / "auth"),
                "api-keys": [CLIENT_KEY], "ws-auth": True, "request-retry": 0, "max-retry-interval": 1,
                "routing": {"strategy": "fill-first"},
                "streaming": {"bootstrap-retries": 1},
                "remote-management": {"disable-control-panel": True},
                "openai-compatibility": [
                    {"name": "primary", "base-url": f"http://127.0.0.1:{self.upstream.server_port}/primary/v1",
                     "api-key-entries": [{"api-key": UPSTREAM_KEY}],
                     "models": [{"name": "mock-model", "alias": "test-model"},
                                {"name": "mock-failover-model", "alias": "failover-model"}]},
                    {"name": "secondary", "base-url": f"http://127.0.0.1:{self.upstream.server_port}/secondary/v1",
                     "api-key-entries": [{"api-key": UPSTREAM_KEY + "-secondary"}],
                     "models": [{"name": "mock-failover-model", "alias": "failover-model"}]}]}
            path = Path(temp) / "config.yaml"
            path.write_text(json.dumps(config))
            # Prevent diagnostics from ever writing into the user's real proxy directory.
            env = dict(os.environ, HOME=temp, XDG_CONFIG_HOME=temp, XDG_DATA_HOME=temp)
            self.process = subprocess.Popen([os.environ["OMAPROXY_TEST_BINARY"], "--config", str(path),
                "--local-model"], cwd=temp, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.stack.callback(self.stop)
            for _ in range(100):
                if self.process.poll() is not None:
                    raise RuntimeError("Isolated backend exited before readiness")
                try:
                    with self.open("/v1/models") as response:
                        json.load(response)
                    break
                except OSError:
                    time.sleep(0.05)
            else:
                raise RuntimeError("Isolated backend did not become ready")
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def stop(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()

    def open(self, path="/v1/responses", body=None, key=CLIENT_KEY):
        request = urllib.request.Request(self.base + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        return urllib.request.urlopen(request, timeout=8)

    def call(self, body):
        with self.open(body=body) as response:
            return json.load(response)

    def stream(self, body):
        with self.open(body=body) as response:
            if "text/event-stream" not in response.headers.get("Content-Type", ""):
                raise AssertionError("Responses stream did not return text/event-stream")
            return list(read_sse(response))


def read_sse(response):
    """Parse SSE frames; handle comments and multiline data instead of line matching."""
    event, data = None, []
    for raw in response:
        line = raw.decode().rstrip("\r\n")
        if not line:
            if data:
                payload = "\n".join(data)
                if payload != "[DONE]":
                    value = json.loads(payload)
                    if event and event != value.get("type"):
                        raise AssertionError("SSE event name differs from payload type")
                    yield value
            event, data = None, []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        raise AssertionError("SSE connection ended inside a frame")


def request(scenario="text", **fields):
    return {"model": "test-model", "input": [{"role": "user", "content": [
        {"type": "input_text", "text": "fixture:" + scenario}]}], "store": False, **fields}


class ResponsesWebSocket:
    """Small RFC 6455 client: masked writes, fragmentation and ping/pong.

    This only tests the downstream transport. The upstream stays local HTTP SSE.
    """
    def __init__(self, backend, key=CLIENT_KEY):
        self.socket = socket.create_connection(("127.0.0.1", backend.port), timeout=8)
        self.reader = self.socket.makefile("rb")
        nonce = base64.b64encode(os.urandom(16)).decode()
        self.socket.sendall((f"GET /v1/responses HTTP/1.1\r\nHost: 127.0.0.1:{backend.port}\r\n"
            f"Authorization: Bearer {key}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {nonce}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        try:
            self.status = int(self.reader.readline().split()[1])
            headers = {}
            while True:
                line = self.reader.readline()
                if line in (b"\r\n", b"\n"):
                    break
                if not line:
                    raise AssertionError("WebSocket handshake ended inside headers")
                name, value = line.decode().split(":", 1)
                headers[name.lower()] = value.strip()
            if self.status == 101:
                expected = base64.b64encode(hashlib.sha1((nonce +
                    "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                if headers.get("sec-websocket-accept") != expected:
                    raise AssertionError("Invalid WebSocket accept header")
                if headers.get("upgrade", "").lower() != "websocket":
                    raise AssertionError("Missing WebSocket upgrade header")
        except BaseException:
            self.close()
            raise

    def close(self):
        self.reader.close()
        self.socket.close()

    def send_frame(self, opcode, payload):
        size = len(payload)
        header = bytes([0x80 | opcode])
        if size < 126:
            header += bytes([0x80 | size])
        elif size < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", size)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", size)
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.socket.sendall(header + mask + masked)

    def send(self, value):
        self.send_frame(1, json.dumps(value).encode())

    def exact(self, size):
        data = self.reader.read(size)
        if len(data) != size:
            raise AssertionError("WebSocket disconnected inside a frame")
        return data

    def receive(self):
        fragments = bytearray()
        while True:
            first, second = self.exact(2)
            if second & 0x80:
                raise AssertionError("Server WebSocket frame must be unmasked")
            size = second & 0x7f
            if size == 126:
                size = struct.unpack("!H", self.exact(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self.exact(8))[0]
            if size > 1024 * 1024:
                raise AssertionError("Fixture WebSocket frame exceeded 1 MiB")
            payload = self.exact(size)
            opcode = first & 0x0f
            if opcode == 9:
                self.send_frame(10, payload)
                continue
            if opcode == 10:
                continue
            if opcode == 8:
                raise AssertionError("WebSocket closed before terminal response: " + repr(payload))
            if opcode not in (0, 1):
                raise AssertionError("Expected text or continuation frame")
            fragments.extend(payload)
            if first & 0x80:
                return json.loads(fragments)

    def response(self, body):
        self.send({"type": "response.create", **body})
        events = []
        for _ in range(100):
            event = self.receive()
            events.append(event)
            if event.get("type") in ("response.completed", "response.failed", "response.incomplete", "error"):
                return events
        raise AssertionError("WebSocket response exceeded 100 events")
