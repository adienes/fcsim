"""Opt-in real CLI compatibility check. No provider requests or model charges.

Run with FCSIM_TEST_CODEX=1 python -m pytest tests/test_codex_cli.py.
The installed CLI talks to a local fake Responses endpoint and the real MCP server.
"""

import json
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fcsim.agents import CodexRunner
from fcsim.mcp import McpServer


pytestmark = pytest.mark.skipif(os.environ.get("FCSIM_TEST_CODEX") != "1" or not shutil.which("codex"),
                                reason="opt-in check needs a local Codex CLI")


def test_codex_can_call_only_its_granted_mcp_tools_and_return_json():
    requests, tool_calls = [], []
    answer = {"ok": True, "note": None}

    class ModelHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"models": []}')

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            if len(requests) == 1:
                namespace = next((t for t in request["tools"] if t.get("type") == "namespace"), None)
                tool = namespace["tools"][0]["name"] if namespace else next(
                    t["name"] for t in request["tools"] if t.get("name", "").endswith("get_view"))
                item = {"type": "function_call", "id": "fc_test", "call_id": "call_test",
                        "name": tool, "arguments": "{}", "status": "completed"}
                if namespace:
                    item["namespace"] = namespace["name"]
            else:
                item = {"type": "message", "role": "assistant", "id": "msg_test", "status": "completed",
                        "content": [{"type": "output_text", "text": json.dumps(answer), "annotations": []}]}
            response = {"id": f"resp_{len(requests)}", "object": "response", "status": "completed",
                        "output": [item], "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}}
            events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                      {"type": "response.output_item.added", "output_index": 0, "item": item},
                      {"type": "response.output_item.done", "output_index": 0, "item": item},
                      {"type": "response.completed", "response": response}]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in events:
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())

    model = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    threading.Thread(target=model.serve_forever, daemon=True).start()

    class LocalCodex(CodexRunner):
        def _command(self, *args):
            command = super()._command(*args)
            options = {"model_provider": "local_test", "model_providers.local_test.name": "Local test",
                       "model_providers.local_test.base_url": f"http://127.0.0.1:{model.server_port}/v1",
                       "model_providers.local_test.wire_api": "responses",
                       "model_providers.local_test.requires_openai_auth": False,
                       "features.enable_request_compression": False}
            extra = [arg for key, value in options.items() for arg in ("--config", f"{key}={json.dumps(value)}")]
            return command[:-1] + extra + command[-1:]

    def call(request_id, name, arguments):
        tool_calls.append((name, arguments))
        return {"seat": 0, "hand": ["Island"]}

    definitions = [{"name": name, "description": name,
                    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}
                   for name in ("get_view", "not_granted")]
    try:
        with McpServer(lambda token: (definitions, call) if token == "seat-zero" else None) as mcp:
            runner = LocalCodex(model="fcsim-test-model", timeout=30)
            reply = runner.ask("You pilot seat zero. Use get_view then answer with JSON.", "Begin.",
                               tools=mcp.connection("seat-zero", ("get_view",)),
                               schema={"type": "object", "additionalProperties": False, "required": ["ok"],
                                       "properties": {"ok": {"type": "boolean"}, "note": {"type": "string"}}})
        assert reply.data == {"ok": True}
        assert tool_calls == [("get_view", {})]
        assert len(requests) == 2
        for request in requests:
            # Codex includes generic MCP discovery and an inert Plan-mode helper.
            # Our server has no resources; neither helper grants additional access.
            helpers = {"request_user_input", "list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"}
            offered = [tool["name"] for group in request["tools"] for tool in group.get("tools", [group])]
            assert all(name.endswith("get_view") or name in helpers for name in offered), offered
            assert any(name.endswith("get_view") for name in offered)
        assert any(item.get("type") == "function_call_output" and "Island" in json.dumps(item["output"])
                   for item in requests[-1]["input"])
        assert reply.cost_usd is None and runner.budget.tokens["output_tokens"] > 0
    finally:
        model.shutdown()
        model.server_close()
