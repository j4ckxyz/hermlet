"""A tiny OpenAI-compatible server so a real Hermes can run a turn with no keys and no cost.

``POST /v1/chat/completions`` is a scripted model: it echoes the user, and a
message that mentions the lamp calls the ``hermlet_action`` tool (through
Hermes's tool-search bridge when the tool is not offered directly).
``POST /v1/audio/transcriptions`` returns ``TRANSCRIPT``.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "fake-hermlet-model"
TRANSCRIPT = "what is on my shopping list"


def _last_user_text(messages: list) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, list):
                return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            return str(content or "")
    return ""


def plan_reply(body: dict) -> dict:
    messages = body.get("messages") or []
    tools = {t.get("function", {}).get("name") for t in body.get("tools") or []}
    if messages and messages[-1].get("role") == "tool":
        result = str(messages[-1].get("content") or "")
        ok = '"success": true' in result.replace('\\"', '"')
        return {"content": "The lamp is on now." if ok else f"That did not work: {result[:160]}"}
    text = _last_user_text(messages)
    if "lamp" in text.lower():
        call = ("hermlet_action", {"action": "lamp.set", "args": {"on": True}})
        if call[0] in tools:
            return {"tool_call": call}
        if "tool_call" in tools:
            return {"tool_call": ("tool_call", {"calls": [{"name": call[0], "arguments": call[1]}]})}
    said = " ".join(line for line in text.strip().splitlines() if line.strip() and not line.startswith("["))
    return {"content": f"Echo: {said[:300]}"}


class Handler(BaseHTTPRequestHandler):
    requests_log: list = []

    def log_message(self, *args):
        pass

    def _json(self, code: int, obj) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "test"}]})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        path = self.path.split("?")[0].rstrip("/")
        if path.endswith("/chat/completions"):
            body = json.loads(raw or b"{}")
            Handler.requests_log.append({"user": _last_user_text(body.get("messages") or []),
                                         "raw_last": json.dumps((body.get("messages") or [{}])[-1])[:2000]})
            self._chat(body)
        elif path.endswith("/audio/transcriptions"):
            self._json(200, {"text": TRANSCRIPT})
        else:
            self._json(404, {"error": {"message": f"unsupported path {path}"}})

    def _chat(self, body: dict) -> None:
        plan = plan_reply(body)
        cid, created = "chatcmpl-" + uuid.uuid4().hex[:12], int(time.time())
        if plan.get("tool_call"):
            name, args = plan["tool_call"]
            call = {"id": "call_" + uuid.uuid4().hex[:8], "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)}}
            message, finish = {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"
        else:
            message, finish = {"role": "assistant", "content": plan["content"]}, "stop"
        usage = {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}
        if not body.get("stream"):
            self._json(200, {"id": cid, "object": "chat.completion", "created": created, "model": MODEL,
                             "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(delta: dict, finish_reason=None, extra=None):
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL,
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
            chunk.update(extra or {})
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.flush()

        emit({"role": "assistant", "content": ""})
        if message.get("tool_calls"):
            call = message["tool_calls"][0]
            emit({"tool_calls": [{"index": 0, "id": call["id"], "type": "function",
                                  "function": {"name": call["function"]["name"], "arguments": ""}}]})
            emit({"tool_calls": [{"index": 0, "function": {"arguments": call["function"]["arguments"]}}]})
        else:
            for i, word in enumerate(message["content"].split(" ")):
                emit({"content": word if i == 0 else " " + word})
                time.sleep(0.01)
        emit({}, finish, {"usage": usage})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


def start(port: int = 0):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]
