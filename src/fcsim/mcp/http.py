"""Serve tools to MCP clients over the streamable HTTP transport, answering in plain JSON.

Only what a tool server needs is implemented: initialize, tools/list, tools/call and ping. Every
request must carry a bearer token; the token decides which tools the caller gets and on whose
behalf they act, so an agent cannot claim to be another seat.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .connection import McpConnection

PROTOCOL_VERSION = "2025-06-18"


class McpServer:
    """`tools_for(token)` returns (definitions, call) for a valid token, or None to refuse it.
    `call(request_id, name, arguments)` returns a JSON-compatible result."""

    def __init__(self, tools_for: Callable[[str], tuple[list[dict], Callable] | None], *, name: str = "fcsim",
                 host: str = "127.0.0.1", port: int = 0):
        self.tools_for, self.name = tools_for, name
        self._lock = threading.Lock()  # one call at a time: the table is not built for concurrent writers
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _send(self, status: int, body: dict | None = None) -> None:
                data = json.dumps(body).encode() if body is not None else b""
                try:
                    self.send_response(status)
                    if data:
                        self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the model's process died while its call was being answered (an API error killed it): nobody to answer

            def do_GET(self) -> None:  # no server-initiated stream is offered
                self._send(405)

            def do_DELETE(self) -> None:
                self._send(200)

            def do_POST(self) -> None:
                token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                tools = server.tools_for(token) if token else None
                if tools is None:
                    return self._send(401, {"error": "unknown or expired token"})
                try:
                    message = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                except ValueError:
                    return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                if isinstance(message, list) or "id" not in message:  # a notification: nothing to answer
                    return self._send(202)
                self._send(200, {"jsonrpc": "2.0", "id": message["id"], **server._answer(message, *tools)})

        self._http = ThreadingHTTPServer((host, port), Handler)
        self.url = f"http://{host}:{self._http.server_address[1]}/mcp"
        self._thread = threading.Thread(target=self._http.serve_forever, daemon=True)

    def _answer(self, message: dict, definitions: list[dict], call: Callable) -> dict:
        method, params = message.get("method"), message.get("params") or {}
        if method == "initialize":
            return {"result": {"protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                               "capabilities": {"tools": {}}, "serverInfo": {"name": self.name, "version": "0.1"}}}
        if method == "ping":
            return {"result": {}}
        if method == "tools/list":
            return {"result": {"tools": definitions}}
        if method == "tools/call":
            if params.get("name") not in {d["name"] for d in definitions}:
                return {"error": {"code": -32602, "message": f"Unknown tool: {params.get('name')}"}}
            try:
                with self._lock:
                    result = call(str(message["id"]), params["name"], params.get("arguments") or {})
            except Exception as error:  # a bug in a tool must reach the caller as an error, not a dropped connection
                result = {"error": {"code": "internal_error", "message": f"{type(error).__name__}: {error}"}}
            return {"result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                               "isError": isinstance(result, dict) and "error" in result}}
        return {"error": {"code": -32601, "message": f"Method not found: {method}"}}

    def start(self) -> McpServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._http.shutdown()
        self._http.server_close()

    def __enter__(self) -> McpServer:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def connection(self, token: str, allowed_tools: tuple[str, ...]) -> McpConnection:
        """Grant a runner access to this wake's tools; the runner handles client configuration."""
        return McpConnection(self.name, self.url, {"Authorization": f"Bearer {token}"}, allowed_tools)
