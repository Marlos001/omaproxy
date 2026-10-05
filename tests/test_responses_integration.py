"""Responses contracts used by coding clients, against a real isolated backend.

Run with OMAPROXY_TEST_BINARY=/absolute/path/to/cli-proxy-api. These are protocol
checks, not a live Codex/T3 session or provider authentication acceptance test.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import unittest
import urllib.error

from responses_fixture import ARGUMENTS, REASONING, ResponsesBackend, ResponsesWebSocket, TEXT, UPSTREAM_KEY, read_sse, request

TOOLS = [{"type": "function", "name": "read_file", "description": "Read a file",
          "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                         "required": ["path"], "additionalProperties": False}, "strict": True},
         {"type": "function", "name": "list_files", "parameters": {
             "type": "object", "properties": {"directory": {"type": "string"}},
             "required": ["directory"], "additionalProperties": False}, "strict": True}]


@unittest.skipUnless(os.environ.get("OMAPROXY_TEST_BINARY"), "set OMAPROXY_TEST_BINARY for Responses contracts")
class ResponsesIntegration(unittest.TestCase):
    def setUp(self):
        self.backend = self.enterContext(ResponsesBackend())

    def assert_completed_stream(self, events):
        kinds = [event["type"] for event in events]
        self.assertEqual(kinds.count("response.created"), 1)
        self.assertEqual(kinds.count("response.completed"), 1)
        self.assertEqual(kinds[-1], "response.completed")
        self.assertLess(kinds.index("response.created"), kinds.index("response.completed"))
        self.assertNotIn("response.failed", kinds)
        sequences = [event["sequence_number"] for event in events]
        self.assertEqual(sequences, sorted(set(sequences)))
        created = next(event["response"] for event in events if event["type"] == "response.created")
        completed = events[-1]["response"]
        self.assertEqual(created["id"], completed["id"])
        self.assertEqual(created["status"], "in_progress")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["usage"]["input_tokens"], 11)
        self.assertEqual(completed["usage"]["output_tokens"], 7)
        self.assertEqual(completed["usage"]["total_tokens"], 18)
        for index, item in enumerate(completed["output"]):
            added = [event for event in events if event["type"] == "response.output_item.added"
                     and event["output_index"] == index]
            done = [event for event in events if event["type"] == "response.output_item.done"
                    and event["output_index"] == index]
            self.assertEqual(len(added), 1)
            self.assertEqual(len(done), 1)
            self.assertEqual(added[0]["item"]["id"], item["id"])
            terminal_item = dict(done[0]["item"])
            # encrypted_content is optional. The Chat bridge emits an empty
            # value in reasoning item.done and omits it from response.output.
            if terminal_item.get("encrypted_content") == "" and "encrypted_content" not in item:
                terminal_item.pop("encrypted_content")
            self.assertEqual(terminal_item, item)
        return completed

    def test_client_auth_model_alias_and_nonstream_response(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.backend.open(body=request(), key="wrong-key")
        self.assertEqual(error.exception.code, 401)
        error.exception.close()
        self.assertEqual(self.backend.received, [])
        result = self.backend.call(request(instructions="Use this fake coding fixture."))
        self.assertEqual(result["object"], "response")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output"][0]["content"][0]["text"], TEXT)
        self.assertEqual(result["usage"]["total_tokens"], 18)
        path, key, body = self.backend.received[-1]
        self.assertEqual(path, "/primary/v1/chat/completions")
        self.assertEqual(key, "Bearer " + UPSTREAM_KEY)
        self.assertEqual(body["model"], "mock-model")
        self.assertIn("Use this fake coding fixture.", [msg["content"] for msg in body["messages"]])

    def test_text_stream_lifecycle_item_ids_and_usage(self):
        events = self.backend.stream(request(stream=True))
        completed = self.assert_completed_stream(events)
        deltas = [event for event in events if event["type"] == "response.output_text.delta"]
        self.assertEqual("".join(event["delta"] for event in deltas), TEXT)
        item = completed["output"][0]
        self.assertEqual(item["content"][0]["text"], TEXT)
        self.assertTrue(all(event["item_id"] == item["id"] for event in deltas))
        self.assertTrue(all(event["output_index"] == 0 and event["content_index"] == 0 for event in deltas))
        done = next(event for event in events if event["type"] == "response.output_text.done")
        self.assertEqual(done["text"], TEXT)
        self.assertEqual(done["item_id"], item["id"])
        self.assertEqual(completed["usage"]["input_tokens_details"]["cached_tokens"], 3)
        self.assertEqual(completed["usage"]["output_tokens_details"]["reasoning_tokens"], 2)

    def test_reasoning_summary_is_separate_from_answer(self):
        events = self.backend.stream(request("reasoning", stream=True,
            reasoning={"effort": "medium", "summary": "auto"}))
        completed = self.assert_completed_stream(events)
        reasoning = [event for event in events if event["type"] == "response.reasoning_summary_text.delta"]
        self.assertEqual("".join(event["delta"] for event in reasoning), REASONING)
        items = {item["type"]: item for item in completed["output"]}
        self.assertEqual(items["reasoning"]["summary"][0]["text"], REASONING)
        self.assertEqual(items["message"]["content"][0]["text"], TEXT)
        self.assertTrue(all(event["item_id"] == items["reasoning"]["id"] for event in reasoning))
        done = next(event for event in events if event["type"] == "response.reasoning_summary_text.done")
        self.assertEqual(done["text"], REASONING)
        self.assertEqual(done["item_id"], items["reasoning"]["id"])

    def test_tool_arguments_and_explicit_tool_result_round_trip(self):
        events = self.backend.stream(request("tools", stream=True, tools=TOOLS,
            tool_choice="auto", parallel_tool_calls=True))
        completed = self.assert_completed_stream(events)
        call = completed["output"][0]
        self.assertEqual(call["type"], "function_call")
        self.assertEqual(call["name"], "read_file")
        self.assertEqual(json.loads(call["arguments"]), {"path": "README.md"})
        deltas = [event for event in events if event["type"] == "response.function_call_arguments.delta"]
        self.assertEqual("".join(event["delta"] for event in deltas), ARGUMENTS)
        self.assertTrue(all(event["item_id"] == call["id"] for event in deltas))
        done = next(event for event in events if event["type"] == "response.function_call_arguments.done")
        self.assertEqual(done["arguments"], ARGUMENTS)
        self.assertEqual(done["item_id"], call["id"])
        followup = request("tools", tools=TOOLS)
        followup["input"] += [call, {"type": "function_call_output", "call_id": call["call_id"],
                                       "output": "Fixture README contents."}]
        result = self.backend.call(followup)
        self.assertEqual(result["output"][0]["content"][0]["text"], "Tool result received.")
        translated = self.backend.received[-1][2]
        tool = next(msg for msg in translated["messages"] if msg["role"] == "tool")
        assistant = next(msg for msg in translated["messages"] if msg["role"] == "assistant")
        self.assertEqual(tool["tool_call_id"], call["call_id"])
        self.assertEqual(assistant["tool_calls"][0]["id"], tool["tool_call_id"])
        self.assertEqual(tool["content"], "Fixture README contents.")
        self.assertEqual(translated["tools"][0]["function"]["parameters"], TOOLS[0]["parameters"])

    def test_parallel_tools_keep_call_identity_and_arguments(self):
        events = self.backend.stream(request("parallel-tools", stream=True, tools=TOOLS, parallel_tool_calls=True))
        completed = self.assert_completed_stream(events)
        calls = completed["output"]
        self.assertEqual([call["name"] for call in calls], ["read_file", "list_files"])
        self.assertEqual(len({call["call_id"] for call in calls}), 2)
        for call, expected in zip(calls, [{"path": "README.md"}, {"directory": "."}]):
            deltas = [event for event in events if event["type"] == "response.function_call_arguments.delta"
                      and event["item_id"] == call["id"]]
            self.assertEqual(json.loads("".join(event["delta"] for event in deltas)), expected)
            self.assertEqual(json.loads(call["arguments"]), expected)

    def test_structured_output_schema_and_image_input_translation(self):
        image = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQAAAABJRU5ErkJggg=="
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}},
                  "required": ["ok"], "additionalProperties": False}
        body = request("structured", text={"format": {"type": "json_schema", "name": "fixture",
                                                       "schema": schema, "strict": True}})
        body["input"][0]["content"].append({"type": "input_image", "image_url": image, "detail": "low"})
        result = self.backend.call(body)
        self.assertEqual(json.loads(result["output"][0]["content"][0]["text"]), {"ok": True})
        upstream = self.backend.received[-1][2]
        self.assertEqual(upstream["response_format"]["json_schema"]["schema"], schema)
        self.assertTrue(upstream["response_format"]["json_schema"]["strict"])
        parts = upstream["messages"][-1]["content"]
        self.assertIn({"type": "image_url", "image_url": {"url": image, "detail": "low"}}, parts)

    def test_concurrent_streams_do_not_mix_ids_text_or_usage(self):
        def run(index):
            scenario = "concurrent-" + str(index)
            events = self.backend.stream(request(scenario, stream=True))
            completed = self.assert_completed_stream(events)
            text = "".join(event["delta"] for event in events if event["type"] == "response.output_text.delta")
            self.assertEqual(text, scenario)
            self.assertEqual(completed["output"][0]["content"][0]["text"], scenario)
            return completed["id"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(run, range(4)))
        self.assertEqual(len(set(ids)), 4)
        self.assertEqual(len(self.backend.received), 4)

    def test_failover_before_first_committed_stream_event(self):
        events = self.backend.stream(request("retry", model="failover-model", stream=True))
        completed = self.assert_completed_stream(events)
        self.assertEqual(completed["output"][0]["content"][0]["text"], TEXT)
        self.assertEqual([path for path, _, _ in self.backend.received],
                         ["/primary/v1/chat/completions", "/secondary/v1/chat/completions"])
        self.assertTrue(all("fixture unavailable" not in json.dumps(event) for event in events))

    def test_disconnect_cancels_upstream_and_backend_remains_usable(self):
        response = self.backend.open(body=request("hold", stream=True))
        try:
            events = read_sse(response)
            for event in events:
                if event["type"] == "response.output_text.delta":
                    break
            self.assertTrue(self.backend.hold_started.wait(1))
        finally:
            response.close()
        self.assertTrue(self.backend.cancelled.wait(3), "client disconnect did not close the upstream stream")
        self.assertEqual(self.backend.call(request())["status"], "completed")

    def test_interrupted_upstream_does_not_report_success(self):
        events = self.backend.stream(request("interrupted", stream=True))
        self.assertTrue(any(event["type"] == "response.output_text.delta" for event in events))
        self.assertFalse(any(event["type"] == "response.completed" for event in events),
                         "truncated upstream must not become a completed Responses response")
        self.assertTrue(any(event["type"] in ("error", "response.failed", "response.incomplete") for event in events),
                        "truncated stream needs a terminal error clients can observe")

    def test_websocket_auth_and_tool_result_on_same_connection(self):
        websocket = ResponsesWebSocket(self.backend)
        self.addCleanup(websocket.close)
        if websocket.status == 404:
            self.skipTest("backend has no downstream /v1/responses WebSocket route")
        self.assertEqual(websocket.status, 101, "backend did not accept authenticated WebSocket")
        unauthorized = ResponsesWebSocket(self.backend, key="wrong-key")
        self.addCleanup(unauthorized.close)
        self.assertEqual(unauthorized.status, 401)
        self.assertEqual(self.backend.received, [])
        first = self.assert_completed_stream(websocket.response(request("tools", tools=TOOLS)))
        call = first["output"][0]
        self.assertEqual(call["type"], "function_call")
        self.assertEqual(json.loads(call["arguments"]), {"path": "README.md"})
        followup = request("tools", tools=TOOLS, previous_response_id=first["id"])
        followup["input"] = [{"type": "function_call_output", "call_id": call["call_id"],
                              "output": "Fixture README contents."}]
        second = self.assert_completed_stream(websocket.response(followup))
        self.assertEqual(second["output"][0]["content"][0]["text"], "Tool result received.")
        upstream = self.backend.received[-1][2]
        tool = next(msg for msg in upstream["messages"] if msg["role"] == "tool")
        assistant = next(msg for msg in upstream["messages"] if msg["role"] == "assistant")
        self.assertEqual(tool["tool_call_id"], call["call_id"])
        self.assertEqual(assistant["tool_calls"][0]["id"], call["call_id"])
        self.assertEqual(tool["content"], "Fixture README contents.")


if __name__ == "__main__":
    unittest.main()
