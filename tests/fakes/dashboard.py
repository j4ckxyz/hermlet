"""A small stand-in for the Hermes dashboard: just the parts a hermlet uses.

REST (sign-in, status, sessions, transcription) on one port and the ``/api/ws``
JSON-RPC stream on another. The shapes follow Hermes's own contract
(``apps/shared/src/gateway-contract.openrpc.json``); a live run against a real
Hermes is what proves them, this fake keeps the mapping from drifting.
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class FakeDashboard:
    def __init__(self, username: str = "ada", password: str = "pw", auth_required: bool = True):
        self.username, self.password, self.auth_required = username, password, auth_required
        self.install_id = "fake-install-1"
        self.access = ""                       # the cookie value issued most recently
        self.accesses: set = set()             # every cookie value still accepted (several devices sign in)
        self.tickets: set = set()
        self.sessions: dict = {                # stored id -> row
            "disc-1": {"id": "disc-1", "source": "discord", "title": "Trip planning", "started_at": 100.0,
                       "last_active": 200.0, "message_count": 2, "preview": "Find me a train"},
            "cron-1": {"id": "cron-1", "source": "cron", "title": "Nightly backup", "started_at": 50.0,
                       "last_active": 60.0, "message_count": 1, "preview": "ran"},
        }
        self.messages: dict = {
            "disc-1": [{"id": 1, "role": "user", "content": "Find me a train", "timestamp": 100.0},
                       {"id": 2, "role": "tool", "content": "{}", "timestamp": 101.0},
                       {"id": 3, "role": "assistant", "content": "There is one at 9:40.", "timestamp": 102.0}],
            "cron-1": [{"id": 4, "role": "assistant", "content": "ran", "timestamp": 60.0}],
        }
        self.live: dict = {}                   # runtime id -> stored id
        self.rpc_log: list = []
        self.pending_images: dict = {}
        # A little of the Hermes host's disk, for /api/fs: where the critter and the agent's notes live.
        self.home = "/srv/hermes"
        self.dirs: set = {"/srv", self.home, self.home + "/memories"}
        self.files: dict = {self.home + "/memories/MEMORY.md": "User likes short answers\n\u00a7\nThe server runs Ubuntu"}
        self.memory_settings: dict = {"memory_enabled": True, "memory_char_limit": 2200}
        self.transcript = "what is the weather"
        self.stt_error = ""
        self.connections: list = []
        self._http = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.http_port = self._http.server_address[1]
        self.ws_port = 0
        self._ws_server = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.http_port}"

    async def start(self) -> None:
        from websockets.asyncio.server import serve

        threading.Thread(target=self._http.serve_forever, daemon=True).start()
        self._ws_server = await serve(self._ws, "127.0.0.1", 0, max_size=32 * 1024 * 1024)
        self.ws_port = self._ws_server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._http.shutdown()
        self._http.server_close()
        if self._ws_server:
            self._ws_server.close()
            await self._ws_server.wait_closed()

    def expire_session(self) -> None:
        """What a 30-day-unused refresh token looks like: every cookie is refused."""
        self.access = "revoked-" + uuid.uuid4().hex
        self.accesses.clear()

    # -- REST -------------------------------------------------------------------------

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code: int, body, cookies=()):
                data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/html" if isinstance(body, str) else "application/json")
                self.send_header("Content-Length", str(len(data)))
                for cookie in cookies:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                self.wfile.write(data)

            def _authed(self) -> bool:
                if not fake.auth_required:
                    return self.headers.get("X-Hermes-Session-Token") == "page-token"
                cookie = self.headers.get("Cookie") or ""
                return any(f"hermes_session_at={value}" in cookie for value in fake.accesses)

            def _body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"{}")

            def do_GET(self):
                url = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                if url.path == "/api/status":
                    return self._send(200, {"version": "9.9.9", "install_id": fake.install_id,
                                            "auth_required": fake.auth_required, "auth_providers": ["basic"]})
                if url.path == "/" and not fake.auth_required:
                    return self._send(200, '<script>window.__HERMES_SESSION_TOKEN__="page-token";</script>')
                if not self._authed():
                    return self._send(401, {"detail": "Unauthorized"})
                if url.path == "/api/config":
                    return self._send(200, {"memory": fake.memory_settings})
                if url.path == "/api/fs/read-text":
                    if query.get("path") not in fake.files:
                        return self._send(404, {"detail": "File not found"})
                    return self._send(200, {"text": fake.files[query["path"]], "truncated": False, "path": query["path"]})
                if url.path == "/api/sessions":
                    rows = sorted(fake.sessions.values(), key=lambda r: -r["last_active"])
                    if query.get("source"):
                        rows = [r for r in rows if r["source"] == query["source"]]
                    excluded = (query.get("exclude_sources") or "").split(",")
                    rows = [r for r in rows if r["source"] not in excluded]
                    start = int(query.get("offset") or 0)
                    return self._send(200, {"sessions": rows[start:start + int(query.get("limit") or 20)], "total": len(rows)})
                if url.path == "/api/sessions/search":
                    q = (query.get("q") or "").lower()
                    hits = [{"session_id": sid, "source": fake.sessions[sid]["source"], "snippet": m["content"],
                             "session_started": fake.sessions[sid]["started_at"]}
                            for sid, msgs in fake.messages.items() for m in msgs if q in m["content"].lower()]
                    return self._send(200, {"results": hits})
                parts = url.path.split("/")
                if len(parts) >= 4 and parts[1:3] == ["api", "sessions"] and parts[3] in fake.sessions:
                    if len(parts) == 5 and parts[4] == "messages":
                        return self._send(200, {"session_id": parts[3], "messages": fake.messages.get(parts[3], [])})
                    if len(parts) == 4:
                        return self._send(200, fake.sessions[parts[3]])
                return self._send(404, {"detail": "Not found"})

            def do_POST(self):
                body = self._body()
                if self.path == "/auth/password-login":
                    if body.get("username") != fake.username or body.get("password") != fake.password:
                        return self._send(401, {"detail": "Invalid credentials"})
                    fake.access = uuid.uuid4().hex
                    fake.accesses.add(fake.access)
                    return self._send(200, {"ok": True, "next": "/"}, cookies=[
                        f"hermes_session_at={fake.access}; Path=/; HttpOnly; Max-Age=43200",
                        "hermes_session_rt=refresh-1; Path=/; HttpOnly; Max-Age=2592000"])
                if not self._authed():
                    return self._send(401, {"detail": "Unauthorized"})
                if self.path == "/api/auth/ws-ticket":
                    ticket = uuid.uuid4().hex
                    fake.tickets.add(ticket)
                    # Hermes rotates the access cookie now and then; a device must keep the new one.
                    fake.access = uuid.uuid4().hex
                    fake.accesses.add(fake.access)
                    return self._send(200, {"ticket": ticket, "ttl_seconds": 30},
                                      cookies=[f"hermes_session_at={fake.access}; Path=/; HttpOnly; Max-Age=43200"])
                if self.path == "/api/fs/write-text":
                    if body["path"].rsplit("/", 1)[0] not in fake.dirs:
                        return self._send(400, {"detail": "Parent directory does not exist"})
                    fake.files[body["path"]] = body["content"]
                    return self._send(200, {"ok": True, "path": body["path"], "byteSize": len(body["content"])})
                if self.path == "/api/files/mkdir":
                    parts = body["path"].strip("/").split("/")
                    for i in range(1, len(parts) + 1):
                        fake.dirs.add("/" + "/".join(parts[:i]))
                    return self._send(200, {"ok": True})
                if self.path == "/api/audio/transcribe":
                    if fake.stt_error:
                        return self._send(400, {"detail": fake.stt_error})
                    assert body["data_url"].startswith("data:audio/wav;base64,")
                    return self._send(200, {"ok": True, "transcript": fake.transcript, "provider": "fake"})
                return self._send(404, {"detail": "Not found"})

        return Handler

    # -- /api/ws ------------------------------------------------------------------------

    async def _ws(self, ws) -> None:
        query = {k: v[0] for k, v in parse_qs(urlsplit(ws.request.path).query).items()}
        if self.auth_required:
            if query.get("ticket") not in self.tickets:
                await ws.close(4401, "bad ticket")
                return
            self.tickets.discard(query["ticket"])
        elif query.get("token") != "page-token":
            await ws.close(4401, "bad token")
            return
        self.connections.append(ws)
        await self.event(ws, "gateway.ready", {"skin": {}})
        answers: dict = {}
        try:
            async for raw in ws:
                frame = json.loads(raw)
                if "method" not in frame:  # the answer to a question we asked
                    waiter = answers.pop(frame.get("id"), None)
                    if waiter:
                        waiter.set_result(frame)
                    continue
                self.rpc_log.append((frame["method"], frame.get("params") or {}))
                try:
                    result = await self._method(ws, frame["method"], frame.get("params") or {}, answers)
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": frame["id"], "result": result}))
                except LookupError as exc:
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": frame["id"],
                                              "error": {"code": 4001, "message": str(exc)}}))
        finally:
            self.connections.remove(ws)

    async def event(self, ws, type_: str, payload, session_id=None) -> None:
        params = {"type": type_, "payload": payload}
        if session_id:
            params["session_id"] = session_id
        await ws.send(json.dumps({"jsonrpc": "2.0", "method": "event", "params": params}))

    async def _method(self, ws, method: str, p: dict, answers: dict) -> dict:
        if method == "config.get":
            if p.get("key") != "profile":
                raise LookupError(f"unknown config key: {p.get('key')}")
            return {"home": self.home, "display": "~/.hermes"}
        if method == "client.capabilities":
            return {"server_requests": ["approval", "clarify"]}
        if method == "session.create":
            stored, runtime = f"s-{uuid.uuid4().hex[:6]}", uuid.uuid4().hex[:8]
            seed = p.get("messages") or []
            self.sessions[stored] = {"id": stored, "source": p.get("source") or "tui", "title": "", "started_at": time.time(),
                                     "last_active": time.time(), "message_count": len(seed), "preview": "",
                                     "parent_session_id": p.get("parent_session_id")}
            self.messages[stored] = [{"id": i + 1, "role": m["role"], "content": m["content"], "timestamp": time.time(),
                                      "display_kind": m.get("display_kind")} for i, m in enumerate(seed)]
            self.live[runtime] = stored
            return {"session_id": runtime, "stored_session_id": stored, "message_count": len(seed), "messages": [], "info": {}}
        if method == "session.resume":
            if p["session_id"] not in self.sessions:
                raise LookupError("session not found")
            runtime = uuid.uuid4().hex[:8]
            self.live[runtime] = p["session_id"]
            return {"session_id": runtime, "stored_session_id": p["session_id"], "message_count":
                    len(self.messages.get(p["session_id"], [])), "messages": [], "info": {}, "open_requests": []}
        if method == "session.close":
            self.live.pop(p["session_id"], None)
            return {"closed": True}
        if method == "session.interrupt":
            return {"status": "interrupted"}
        if method == "image.attach_bytes":
            data = base64.b64decode(p["content_base64"])
            self.pending_images.setdefault(p["session_id"], []).append((p.get("filename"), len(data)))
            return {"attached": True, "name": p.get("filename"), "bytes": len(data), "count": 1}
        if method == "file.attach":
            header, _, encoded = p["data_url"].partition(",")
            size = len(base64.b64decode(encoded))
            return {"attached": True, "name": p["name"], "path": f"/srv/{p['name']}", "ref_path": p["name"],
                    "ref_text": f"@file:{p['name']}", "uploaded": True, "_size": size}
        if method == "prompt.submit":
            stored = self.live.get(p["session_id"])
            if stored is None:
                raise LookupError("session not found")
            asyncio.ensure_future(self._turn(ws, p["session_id"], stored, str(p.get("text") or ""), answers))
            return {"status": "streaming", "user_row_id": len(self.messages[stored]) + 1}
        raise LookupError(f"unknown method {method}")

    async def _ask(self, ws, answers: dict, method: str, params: dict) -> dict:
        frame_id = f"srq-{uuid.uuid4().hex[:4]}"
        answers[frame_id] = asyncio.get_running_loop().create_future()
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": frame_id, "method": method, "params": params}))
        return await asyncio.wait_for(answers[frame_id], 10)

    async def _turn(self, ws, runtime: str, stored: str, text: str, answers: dict) -> None:
        log = self.messages[stored]
        log.append({"id": len(log) + 1, "role": "user", "content": text + "\n\n--- Attached Context ---\n\nfile body",
                    "timestamp": time.time()})
        await self.event(ws, "message.start", None, runtime)
        low = text.lower()
        images = self.pending_images.pop(runtime, [])
        if "restyle" in low:
            # What a real agent does with the hermlet skill: edit the look file with its own tools.
            path = self.home + "/plugin-data/hermlet/look.json"
            await self.event(ws, "tool.start", {"tool_id": "t9", "name": "write_file", "preview": path}, runtime)
            look = json.loads(self.files.get(path) or "{}")
            look.update({"accent": "red", "font": "code"})
            self.dirs.update({self.home + "/plugin-data", self.home + "/plugin-data/hermlet"})
            self.files[path] = json.dumps(look)
            await self.event(ws, "tool.complete", {"tool_id": "t9", "name": "write_file", "summary": "wrote"}, runtime)
        if "fail" in low:
            await self.event(ws, "message.complete", {"text": "", "error": "the model is unavailable"}, runtime)
            return
        if "dangerous" in low:
            reply = await self._ask(ws, answers, "approval", {
                "session_id": runtime, "request_id": "r1", "command": "rm -rf build", "description": "delete build output",
                "choices": ["once", "session", "always", "deny"]})
            answer = f"approval: {(reply.get('result') or {}).get('choice')}"
        elif "which" in low:
            reply = await self._ask(ws, answers, "clarify", {"session_id": runtime, "questions": [
                {"qid": "q1", "question": "Tea or coffee?", "choices": ["tea", "coffee"]},
                {"qid": "q2", "question": "Any sugar?"}]})
            answer = f"clarified: {json.dumps((reply.get('result') or {}).get('answers'), sort_keys=True)}"
        else:
            if "search" in low:
                await self.event(ws, "tool.start", {"tool_id": "t1", "name": "web_search", "preview": "weather lisbon"}, runtime)
                await self.event(ws, "tool.complete", {"tool_id": "t1", "name": "web_search", "summary": "3 results"}, runtime)
            answer = f"Echo: {text}" + (f" (+{len(images)} image(s): {images[0][0]}, {images[0][1]} bytes)" if images else "")
        await self.event(ws, "thinking.delta", {"text": "hm"}, runtime)
        for word in answer.split(" "):
            await self.event(ws, "message.delta", {"text": word + " "}, runtime)
        # Chatter for a different conversation on the same Hermes must never leak into this one.
        await self.event(ws, "message.delta", {"text": "SOMEONE ELSE"}, "other-session")
        log.append({"id": len(log) + 1, "role": "assistant", "content": answer, "timestamp": time.time()})
        self.sessions[stored].update(message_count=len(log), last_active=time.time(), title=text[:30])
        await self.event(ws, "session.title", {"session_id": stored, "title": text[:30]})
        await self.event(ws, "message.complete", {"text": answer, "usage": {}}, runtime)
