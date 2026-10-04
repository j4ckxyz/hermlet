"""``DashboardDevice``: a hermlet that talks to Hermes through its own web API.

Nothing is installed on the Hermes side. The device signs in to the Hermes
dashboard (``hermes dashboard``) the way the Hermes desktop app does, then
uses the same two surfaces:

* ``/api/ws``: the TUI-gateway JSON-RPC stream (sessions, prompts, streamed
  replies, tool activity, approvals and questions, attachments);
* REST: ``/api/sessions`` for history from every surface (Discord, the web,
  the CLI, other hermlets), ``/api/audio/transcribe`` and ``/api/audio/speak``
  for voice.

Conversations are ordinary Hermes sessions with ``source: "hermlet"``, stored
by Hermes in its own ``state.db`` next to every other conversation.

Only session cookies are kept on the device (``auth.json``, mode 0600), never
the password: an access token Hermes rotates every 12 hours and a refresh
token good for 30 days of not being used.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import mimetypes
import re
import urllib.error
import urllib.request
import uuid
import wave
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
from urllib.parse import quote, urlencode, urlsplit

from . import audio, config, look as look_mod, persona as persona_mod
from .base import CONNECTING, READY, BaseDevice, FileRef, HermletError, NeedsSignIn, read_ref

log = logging.getLogger("hermlet")

SOURCE = "hermlet"
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
UPLOAD_LIMIT_BYTES = 10 * 1024 * 1024       # sent base64 in one WebSocket frame, which servers cap at 16 MB
WS_MAX_BYTES = 32 * 1024 * 1024
SHARED_POLL_S = 12                           # how often an idle device re-reads the shared look and critter
QUIET_SOURCES = "cron,subagent,kanban,tool"  # automation noise, hidden from history unless asked for
SEED_MESSAGES = 60                           # how much of a conversation is carried over when continued here
SEED_CHARS = 8000
_ATTACHED_CONTEXT = "\n\n--- Attached Context ---"
_TOKEN_RE = re.compile(r'__HERMES_SESSION_TOKEN__\s*=\s*"([^"]+)"')


def normalize_base(raw: str) -> str:
    """``hermes-box.tail1234.ts.net`` or a full URL, as the dashboard's base URL."""
    text = (raw or "").strip().rstrip("/")
    if not text:
        raise ValueError("no Hermes address given")
    if "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"not a Hermes dashboard address: {raw}")
    netloc = parts.netloc
    if parts.scheme == "http" and parts.port is None:
        netloc += ":9119"  # the dashboard's default port
    return f"{parts.scheme}://{netloc}{parts.path.rstrip('/')}"


class _Http:
    """A very small HTTP client: JSON in and out, cookies kept and written back when they rotate."""

    def __init__(self, base: str, store: Path):
        self.base = base
        self._store = store
        self.cookies: Dict[str, str] = {}
        self.token = ""  # ungated dashboards (loopback) hand out a page token instead of cookies
        saved = config.read_json(store, {}) or {}
        if saved.get("base") == base:
            self.cookies = dict(saved.get("cookies") or {})

    def _save(self) -> None:
        self._store.parent.mkdir(parents=True, exist_ok=True)
        config.write_private(self._store, json.dumps({"base": self.base, "cookies": self.cookies}) + "\n")

    def forget(self) -> None:
        self.cookies = {}
        self._save()

    def _request(self, method: str, path: str, body: Any, timeout: float) -> Tuple[int, Any]:
        headers = {"Accept": "application/json", "User-Agent": "hermlet"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if self.token:
            headers["X-Hermes-Session-Token"] = self.token
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=timeout) as response:
                status, raw, response_headers = response.status, response.read(), response.headers
        except urllib.error.HTTPError as exc:
            status, raw, response_headers = exc.code, exc.read(), exc.headers
        changed = False
        for header in response_headers.get_all("Set-Cookie") or []:
            for name, morsel in SimpleCookie(header).items():
                if morsel.value and morsel["max-age"] != "0":
                    changed |= self.cookies.get(name) != morsel.value
                    self.cookies[name] = morsel.value
                elif self.cookies.pop(name, None) is not None:
                    changed = True
        if changed:
            self._save()
        try:
            return status, json.loads(raw) if raw else {}
        except ValueError:
            return status, raw.decode("utf-8", "replace")

    async def call(self, method: str, path: str, body: Any = None, timeout: float = 30) -> Tuple[int, Any]:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self._request, method, path, body, timeout)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ConnectionError(f"cannot reach Hermes at {self.base}: {reason}") from None

    async def get(self, path: str, timeout: float = 30) -> Any:
        status, body = await self.call("GET", path, timeout=timeout)
        return self._checked(status, body, path)

    async def post(self, path: str, body: Any, timeout: float = 30) -> Any:
        status, body = await self.call("POST", path, body, timeout)
        return self._checked(status, body, path)

    @staticmethod
    def _checked(status: int, body: Any, path: str) -> Any:
        if status in (401, 403):
            raise NeedsSignIn("Hermes wants you to sign in again")
        if status >= 400:
            detail = body.get("detail") if isinstance(body, dict) else str(body)[:200]
            raise HermletError(str(detail or f"Hermes answered {status} for {path}"))
        return body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any):  # a redirect to /login means "sign in", not "follow"
        return None


class DashboardDevice(BaseDevice):
    mode = "dashboard"

    def __init__(self, url: Optional[str] = None, name: Optional[str] = None, *,
                 home: Optional[Union[str, Path]] = None, **kw: Any):
        super().__init__(name, home=home, **kw)
        self.url = normalize_base(url or self.cfg.get("url") or "")
        self.http = _Http(self.url, self.home / "auth.json")
        self.limits = {"upload_bytes": UPLOAD_LIMIT_BYTES, "utterance_s": 120}
        saved = config.read_json(self.home / "session.json", {}) or {}
        self.stored_session: str = str(saved.get("id") or "") if saved.get("base") == self.url else ""
        self.session_title = ""
        self.status: Dict[str, Any] = {}
        self._ws = None
        self._sid = ""          # the live (runtime) session id on this connection
        self._calls: Dict[str, asyncio.Future] = {}
        self._n = 0
        self._reply = ""        # the reply streamed so far
        self._busy = False
        self._prompts: Dict[str, Dict[str, Any]] = {}
        self._send_lock = asyncio.Lock()
        self._hermes_home = ""

    @property
    def device_id(self) -> str:
        return f"{SOURCE}:{self.name}"

    # -- signing in -------------------------------------------------------------------

    async def sign_in(self, username: str, password: str) -> None:
        """Sign in to the Hermes dashboard. Only the session it returns is kept, not the password."""
        status = await self.http.get("/api/status")
        providers = [p for p in status.get("auth_providers") or [] if isinstance(p, str)]
        code, body = await self.http.call("POST", "/auth/password-login", {
            "provider": providers[0] if providers else "basic", "username": username, "password": password})
        if code == 401:
            raise HermletError("That username or password is not right")
        if code == 429:
            raise HermletError("Too many attempts; wait a minute and try again")
        if code >= 400:
            detail = body.get("detail") if isinstance(body, dict) else body
            raise HermletError(f"Hermes would not sign in: {detail or code}")
        self.retry_now()

    async def sign_out(self) -> None:
        self.http.forget()
        await self._close()

    # -- lifecycle ------------------------------------------------------------------

    async def _close(self) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def _session(self) -> None:
        from websockets.asyncio.client import connect

        await self._set_state(CONNECTING)
        self.status = await self.http.get("/api/status", timeout=15)
        if not isinstance(self.status, dict) or "version" not in self.status:
            raise HermletError(f"{self.url} does not look like a Hermes dashboard")
        self.agent = "Hermes"
        seed = str(self.status.get("install_id") or self.url)
        if self.persona.get("seed") != seed and self._remember_persona(persona_mod.generate(seed)):
            await self._emit("persona", persona=self.persona)  # a first look; the owner's choices load below
        ws_url = self._ws_url(await self._ws_credential())
        async with connect(ws_url, max_size=WS_MAX_BYTES, open_timeout=20, ping_interval=20, ping_timeout=40) as ws:
            self._ws = ws
            reader = asyncio.ensure_future(self._read(ws))
            try:
                await self._rpc("client.capabilities", server_requests=True)
                await self._load_persona()
                await self._load_look()
                await self._reattach()
                await self._set_state(READY)
                watcher = asyncio.ensure_future(self._watch_shared())
                try:
                    await reader
                finally:
                    watcher.cancel()
            finally:
                reader.cancel()
                self._ws, self._sid = None, ""
                for future in self._calls.values():
                    if not future.done():
                        future.set_exception(HermletError("the connection to Hermes dropped"))
                self._calls.clear()
                if self._busy:
                    self._busy = False
                    await self._emit("turn.end", outcome="failure")

    def _ws_url(self, query: str) -> str:
        parts = urlsplit(self.url)
        return f"{'wss' if parts.scheme == 'https' else 'ws'}://{parts.netloc}{parts.path}/api/ws?{query}"

    async def _ws_credential(self) -> str:
        if self.status.get("auth_required"):
            if not self.http.cookies:
                raise NeedsSignIn("Sign in to your Hermes to connect this device")
            ticket = await self.http.post("/api/auth/ws-ticket", {})
            return urlencode({"ticket": ticket["ticket"]})
        # An open dashboard (bound to loopback) puts a session token in its page instead.
        status, page = await self.http.call("GET", "/")
        match = _TOKEN_RE.search(page if isinstance(page, str) else "")
        if status != 200 or not match:
            raise HermletError("this Hermes dashboard did not offer a way to connect")
        self.http.token = match.group(1)
        return urlencode({"token": self.http.token})

    async def _reattach(self) -> None:
        """Pick the device's conversation back up after a restart or a dropped connection."""
        if not self.stored_session:
            return
        try:
            result = await self._rpc("session.resume", session_id=self.stored_session, source=SOURCE,
                                     omit_messages=True, timeout=60)
        except HermletError as exc:
            log.info("could not resume %s (%s); the next message starts a new conversation", self.stored_session, exc)
            self._remember_session("")
            return
        self._sid = str(result.get("session_id") or "")
        for frame in result.get("open_requests") or []:
            await self._on_request(frame)

    # -- the critter: kept on Hermes, so every device agrees ---------------------------

    async def _shared(self, name: str) -> str:
        """Where a file every device shares lives on the Hermes host."""
        if not self._hermes_home:
            self._hermes_home = str((await self._rpc("config.get", key="profile")).get("home") or "")
        if not self._hermes_home:
            raise HermletError("this Hermes did not say where it keeps its files")
        return f"{self._hermes_home}/plugin-data/hermlet/{name}"

    async def _persona_file(self) -> str:
        return await self._shared("persona.json")

    async def _read_shared(self, name: str) -> Optional[str]:
        status, body = await self.http.call("GET", "/api/fs/read-text?" + urlencode({"path": await self._shared(name)}))
        return str(body.get("text") or "") if status == 200 and isinstance(body, dict) else None

    async def _load_look(self) -> None:
        """Pick up the shared look: changed on another device, in a terminal, or by the agent."""
        try:
            text = await self._read_shared("look.json")
        except (HermletError, ConnectionError) as exc:
            log.debug("could not read the look from Hermes: %s", exc)
            return
        await self._take_look(text or "")

    async def _watch_shared(self) -> None:
        """While connected, notice changes made elsewhere within a few seconds."""
        while True:
            await asyncio.sleep(SHARED_POLL_S)
            if not self._busy:  # during a turn, tool completions trigger the check instead
                await self._load_look()
                await self._load_persona()

    async def set_look(self, changes: Dict[str, Any], this_device_only: bool = False) -> Dict[str, Any]:
        self._require_ready()
        current = look_mod.parse(await self._read_shared("look.json") or "")
        updated = look_mod.merged(current, changes, self.name if this_device_only else None)
        await self._write_text(await self._shared("look.json"), look_mod.dumps(updated))
        await self._take_look(updated)
        return self.look

    async def teach_agent(self) -> bool:
        """Write the skill that tells the agent where the look file is and what goes in it."""
        try:
            await self._shared("look.json")
            await self._write_text(f"{self._hermes_home}/skills/{look_mod.SKILL_NAME}/SKILL.md",
                                   look_mod.skill(f"{self._hermes_home}/plugin-data/hermlet/look.json"))
            if await self._read_shared("look.json") is None:  # give the agent a file to read on its first try
                await self._write_text(await self._shared("look.json"), look_mod.dumps(self.look_file))
            return True
        except (HermletError, ConnectionError) as exc:
            log.info("could not teach Hermes to restyle the app: %s", exc)
            return False

    async def _load_persona(self) -> None:
        """Pick up the name and colour the owner chose, from this or any other device."""
        try:
            path = await self._persona_file()
            status, body = await self.http.call("GET", "/api/fs/read-text?" + urlencode({"path": path}))
            stored = json.loads(body["text"]) if status == 200 and isinstance(body, dict) else None
        except (HermletError, ConnectionError, ValueError, KeyError) as exc:
            log.debug("could not read the critter from Hermes: %s", exc)
            return
        if not isinstance(stored, dict):
            return
        # Redraw from the few facts that matter, so a hand (or agent) edit of the name or colour works.
        seed = str(self.status.get("install_id") or stored.get("seed") or self.url)
        persona = persona_mod.generate(seed, name=stored.get("name"), species=stored.get("species"),
                                       colour=stored.get("colour"), chosen=bool(stored.get("chosen", True)))
        if self._remember_persona(persona):
            await self._emit("persona", persona=self.persona)

    async def _write_text(self, path: str, content: str) -> None:
        status, body = await self.http.call("POST", "/api/fs/write-text", {"path": path, "content": content})
        if status == 400 and "arent directory" in str(body):
            await self.http.post("/api/files/mkdir", {"path": path.rsplit("/", 1)[0]})
            status, body = await self.http.call("POST", "/api/fs/write-text", {"path": path, "content": content})
        self.http._checked(status, body, "write-text")

    async def set_persona(self, name: Optional[str] = None, colour: Optional[str] = None) -> Dict[str, Any]:
        self._require_ready()
        persona = self.draw_persona(name, colour, chosen=True)
        await self._write_text(await self._persona_file(), json.dumps(persona, indent=1))
        if self._remember_persona(persona):
            await self._emit("persona", persona=self.persona)
        return {"persona": persona, "memory": await self._note_in_memory(persona)}

    async def _note_in_memory(self, persona: Dict[str, Any]) -> str:
        """Add one line to the agent's own notes (MEMORY.md), so Hermes knows its critter everywhere."""
        try:
            settings = (await self.http.get("/api/config")).get("memory") or {}
            if settings.get("memory_enabled") is False:
                return "off"
            path = f"{self._hermes_home}/memories/MEMORY.md"
            status, body = await self.http.call("GET", "/api/fs/read-text?" + urlencode({"path": path}))
            if status == 200 and isinstance(body, dict) and body.get("truncated"):
                return "failed"  # never rewrite a file we could not read whole
            current = str(body.get("text") or "") if status == 200 and isinstance(body, dict) else ""
            updated = persona_mod.with_note(current, persona_mod.memory_note(persona),
                                            int(settings.get("memory_char_limit") or 2200))
            if updated is None:
                return "full"
            if updated != current:
                await self._write_text(path, updated)
            return "saved"
        except (HermletError, ConnectionError, ValueError) as exc:
            log.info("could not tell Hermes about the critter: %s", exc)
            return "failed"

    def _remember_session(self, stored: str, title: str = "") -> None:
        self.stored_session, self.session_title = stored, title
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            (self.home / "session.json").write_text(json.dumps({"base": self.url, "id": stored}), encoding="utf-8")
        except OSError:
            pass

    # -- JSON-RPC -------------------------------------------------------------------

    async def _rpc(self, method: str, timeout: float = 30, **params: Any) -> Dict[str, Any]:
        ws = self._ws
        if ws is None:
            raise HermletError("not connected to Hermes")
        self._n += 1
        call_id = f"h{self._n}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._calls[call_id] = future
        try:
            async with self._send_lock:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": call_id, "method": method,
                                          "params": {k: v for k, v in params.items() if v is not None}}))
            reply = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise HermletError(f"Hermes did not answer '{method}' in time") from None
        finally:
            self._calls.pop(call_id, None)
        if "error" in reply:
            error = reply["error"] or {}
            raise HermletError(str(error.get("message") or error))
        return reply.get("result") or {}

    async def _respond(self, frame_id: Any, result: Optional[Dict[str, Any]] = None,
                       error: Optional[Dict[str, Any]] = None) -> None:
        ws = self._ws
        if ws is None:
            return
        body: Dict[str, Any] = {"jsonrpc": "2.0", "id": frame_id}
        body["error" if error else "result"] = error or result or {}
        async with self._send_lock:
            await ws.send(json.dumps(body))

    async def _read(self, ws) -> None:
        async for raw in ws:
            if isinstance(raw, (bytes, bytearray)):
                continue
            for line in raw.splitlines():
                if not line.strip():
                    continue
                try:
                    frame = json.loads(line)
                except ValueError:
                    continue
                if "method" not in frame:
                    future = self._calls.get(str(frame.get("id")))
                    if future is not None and not future.done():
                        future.set_result(frame)
                elif frame["method"] == "event":
                    await self._on_event(frame.get("params") or {})
                elif "id" in frame:
                    await self._on_request(frame)

    # -- what Hermes says -------------------------------------------------------------

    async def _on_event(self, params: Dict[str, Any]) -> None:
        kind, payload = str(params.get("type") or ""), params.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        sid = params.get("session_id")
        if sid and self._sid and sid != self._sid:
            return  # another conversation open on the same Hermes
        if kind == "message.start":
            self._reply, self._busy = "", True
            await self._emit("turn.start")
        elif kind == "message.delta":
            self._reply += str(payload.get("text") or "")
            await self._emit("reply.delta", text=self._reply)
        elif kind in ("thinking.delta", "reasoning.delta"):
            if not self._reply:
                await self._emit("status", text="Thinking")
        elif kind == "tool.start":
            name = str(payload.get("name") or "a tool")
            await self._emit("tool", name=name, state="start", preview=payload.get("preview") or payload.get("context"))
            await self._emit("status", text=_tool_phrase(name, payload))
        elif kind == "tool.complete":
            await self._emit("tool", name=str(payload.get("name") or ""), state="done",
                             summary=payload.get("summary"), duration_s=payload.get("duration_s"))
            await self._emit("status", text="")
            asyncio.ensure_future(self._load_look())  # the agent may just have restyled the app
            asyncio.ensure_future(self._load_persona())
        elif kind == "message.interim":
            self._reply = ""
            await self._emit("reply", text=str(payload.get("text") or ""), interim=True)
        elif kind == "status.update":
            await self._emit("status", text=str(payload.get("text") or ""))
        elif kind == "message.complete":
            text = payload.get("text")
            text = text if isinstance(text, str) else self._reply
            failed = bool(payload.get("error") or payload.get("failure_reason"))
            outcome = "failure" if failed else "cancelled" if payload.get("status") == "interrupted" else "success"
            if text:
                await self._emit("reply", text=text)
            elif failed:
                await self._emit("notice", text=str(payload.get("error") or payload.get("failure_reason")), ttl_s=20)
            self._reply, self._busy = "", False
            await self._emit("status", text="")
            await self._emit("turn.end", outcome=outcome)
            if text and self.cfg.get("speak"):
                asyncio.ensure_future(self._speak(text))
        elif kind == "session.title":
            if payload.get("session_id") in (None, self.stored_session):
                self.session_title = str(payload.get("title") or "")
                await self._emit("session", id=self.stored_session, title=self.session_title)
        elif kind == "error":
            await self._emit("notice", text=str(payload.get("message") or "Something went wrong"), ttl_s=20)
            if self._busy:
                self._busy = False
                await self._emit("turn.end", outcome="failure")
        elif kind == "notice":
            await self._emit("notice", text=str(payload.get("message") or ""), ttl_s=8)
        elif kind == "notification.show":
            ttl = payload.get("ttl_ms")
            await self._emit("notice", text=str(payload.get("text") or ""), ttl_s=(ttl or 8000) / 1000)
        elif kind == "request.cancel":
            await self._withdraw(str(payload.get("id") or ""))

    async def _on_request(self, frame: Dict[str, Any]) -> None:
        """Hermes is asking the person something: an approval, or a question from the agent."""
        frame_id, method = str(frame.get("id")), str(frame.get("method"))
        params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
        if method == "approval":
            choices = [c for c in params.get("choices") or [] if isinstance(c, str)] or ["once", "deny"]
            text = str(params.get("command") or "").strip()
            if params.get("description"):
                text = f"{text}\n{params['description']}".strip()
            self._prompts[frame_id] = {"kind": "approval", "frame": frame_id}
            await self._emit("prompt", id=frame_id, title="Allow this command?", text=text, choices=choices, kind="approval")
        elif method == "clarify":
            questions = [q for q in params.get("questions") or [] if isinstance(q, dict) and q.get("qid")]
            if not questions:
                await self._respond(frame_id, {})
                return
            self._prompts[frame_id] = {"kind": "clarify", "frame": frame_id, "questions": questions, "answers": {}}
            await self._ask_next(frame_id)
        else:
            await self._respond(frame_id, error={"code": -32601, "message": f"a hermlet cannot answer '{method}'"})

    async def _ask_next(self, frame_id: str) -> None:
        entry = self._prompts[frame_id]
        pending = [q for q in entry["questions"] if q["qid"] not in entry["answers"]]
        if not pending:
            self._prompts.pop(frame_id, None)
            await self._respond(frame_id, {"answers": entry["answers"]})
            return
        question = pending[0]
        entry["current"] = question["qid"]
        await self._emit("prompt", id=f"{frame_id}/{question['qid']}", title="Hermes asks", kind="clarify",
                         text=str(question.get("question") or ""), choices=question.get("choices") or None,
                         free_text=not question.get("choices"))

    async def _withdraw(self, frame_id: str) -> None:
        entry = self._prompts.pop(frame_id, None)
        if entry is None:
            return
        shown = f"{frame_id}/{entry['current']}" if entry["kind"] == "clarify" else frame_id
        await self._emit("prompt.close", id=shown)

    async def answer(self, prompt_id: str, answer: str) -> None:
        """Answer a ``prompt``: one of its choices (``yes``/``no`` work for approvals), or free text."""
        frame_id, _, qid = prompt_id.partition("/")
        entry = self._prompts.get(frame_id)
        if entry is None:
            raise HermletError("that question has expired")
        if entry["kind"] == "approval":
            self._prompts.pop(frame_id, None)
            choice = {"yes": "once", "y": "once", "no": "deny", "n": "deny"}.get(answer.strip().lower(), answer.strip())
            await self._respond(frame_id, {"choice": choice})
        else:
            entry["answers"][qid or entry.get("current")] = answer
            await self._ask_next(frame_id)

    # -- talking ----------------------------------------------------------------------

    async def _preamble(self) -> List[Dict[str, Any]]:
        """Hidden opening rows that tell the agent this conversation is on a hermlet, and what
        "change how the app looks" means here. Hermes keeps hidden rows out of its own views."""
        try:
            look_path = await self._shared("look.json")
        except HermletError:
            return []
        note = look_mod.context(self.name, str(self.persona.get("name") or "") if self.look.get("fun", True) else "", look_path)
        return [{"role": "user", "content": note, "display_kind": "hidden"},
                {"role": "assistant", "content": "Understood.", "display_kind": "hidden"}]

    async def _ensure_session(self) -> str:
        if self._sid:
            return self._sid
        created = await self._rpc("session.create", source=SOURCE, messages=await self._preamble(), timeout=60)
        self._sid = str(created["session_id"])
        self._remember_session(str(created.get("stored_session_id") or ""))
        return self._sid

    async def send(self, text: str = "", attachments: Sequence[FileRef] = ()) -> str:
        """Send a message, with any files or images alongside. Returns the message id."""
        self._require_ready()
        sid = await self._ensure_session()
        refs: List[str] = []
        for ref in attachments:
            mention = await self._attach(sid, ref)
            if mention:
                refs.append(mention)
        body = " ".join(refs + [text]).strip()
        if not body and not attachments:
            raise HermletError("nothing to send")
        result = await self._rpc("prompt.submit", session_id=sid, text=body or "Have a look at this.", timeout=60)
        return str(result.get("user_row_id") or uuid.uuid4().hex[:10])

    async def _attach(self, sid: str, ref: FileRef) -> str:
        name, data = await read_ref(ref)
        if len(data) > UPLOAD_LIMIT_BYTES:
            raise HermletError(f"{name} is too large ({len(data) / 1e6:.1f} MB; the limit is "
                               f"{UPLOAD_LIMIT_BYTES / 1e6:.0f} MB)")
        encoded = base64.b64encode(data).decode()
        if name.lower().endswith(IMAGE_EXTS):
            result = await self._rpc("image.attach_bytes", session_id=sid, content_base64=encoded, filename=name, timeout=120)
            if not result.get("attached"):
                raise HermletError(f"Hermes did not accept {name}: {result.get('message') or 'not an image it can read'}")
            return ""  # images ride along with the next message on their own
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        result = await self._rpc("file.attach", session_id=sid, data_url=f"data:{mime};base64,{encoded}", name=name, timeout=120)
        if not result.get("attached"):
            raise HermletError(f"Hermes did not accept {name}")
        return str(result.get("ref_text") or "")

    def voice(self, rate: int = 16000) -> "Utterance":
        """Start a spoken message: ``async with device.voice() as v: await v.write(pcm)``."""
        return Utterance(self, rate)

    async def transcribe(self, wav: bytes) -> str:
        """Speech to text with whatever your Hermes is set up to use."""
        result = await self.http.post("/api/audio/transcribe", {
            "data_url": "data:audio/wav;base64," + base64.b64encode(wav).decode(), "mime_type": "audio/wav"}, timeout=120)
        return str(result.get("transcript") or "").strip()

    async def _speak(self, text: str) -> None:
        try:
            result = await self.http.post("/api/audio/speak", {"text": text[:4000]}, timeout=120)
            _, _, encoded = str(result.get("data_url") or "").partition(",")
            await audio.play_encoded(base64.b64decode(encoded), str(result.get("mime_type") or ""))
        except Exception as exc:  # speaking is a nicety: never let it break a turn
            log.info("could not speak the reply: %s", exc)

    async def cancel(self) -> None:
        """Stop what Hermes is doing for this device."""
        await self.player.abort()
        if self._sid:
            await self._rpc("session.interrupt", session_id=self._sid)

    async def new_session(self) -> None:
        """Start a fresh conversation. The old one stays in Hermes's history."""
        old, self._sid = self._sid, ""
        self._remember_session("")
        if old and self._ws is not None:
            try:
                await self._rpc("session.close", session_id=old)
            except HermletError:
                pass
        await self._emit("session", id="", title="")

    async def event(self, name: str, data: Any = None, notify: bool = False) -> None:
        """Report that something happened. With ``notify`` the agent is told, as a message."""
        if notify:
            detail = "" if data is None else " " + json.dumps(data, separators=(",", ":"))[:500]
            await self.send(f"[{self.name} event] {name}{detail}")

    async def report(self, **sensors: Any) -> None:
        """Keep readings on the device; ask the agent about them with a message."""
        self.sensors.update(sensors)

    # -- history from every surface ---------------------------------------------------

    async def sessions(self, limit: int = 20, offset: int = 0, source: Optional[str] = None,
                       query: Optional[str] = None) -> List[Dict[str, Any]]:
        """Past conversations with this agent from every surface: Discord, the web, other hermlets."""
        if query:
            found = await self.http.get("/api/sessions/search?" + urlencode(
                {"q": query, "limit": limit, **({"source": source} if source else {"exclude_sources": QUIET_SOURCES})}))
            rows = []
            for hit in found.get("results") or []:
                sid = hit.get("session_id") or hit.get("id")
                if sid:
                    rows.append(self._row({"id": sid, "source": hit.get("source"), "title": hit.get("title"),
                                           "preview": hit.get("snippet"), "started_at": hit.get("session_started"),
                                           "last_active": hit.get("last_active")}))
            return rows
        params: Dict[str, Any] = {"limit": min(max(int(limit), 1), 100), "offset": max(int(offset), 0), "order": "recent"}
        if source:
            params["source"] = source
        else:
            params["exclude_sources"] = QUIET_SOURCES
        listing = await self.http.get("/api/sessions?" + urlencode(params))
        return [self._row(r) for r in listing.get("sessions") or []]

    def _row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        preview = _plain(str(row.get("preview") or ""))[:160]
        return {
            "id": row.get("id"), "source": row.get("source") or "", "title": row.get("title") or preview[:60] or "Untitled",
            "preview": preview, "started_at": row.get("started_at") or 0,
            "last_active": row.get("last_active") or row.get("started_at") or 0,
            "message_count": row.get("message_count") or 0, "current": row.get("id") == self.stored_session,
        }

    async def session_messages(self, session_id: str, limit: int = 50, before: Optional[int] = None) -> Dict[str, Any]:
        """One conversation's messages, oldest first: ``{"session": {...}, "messages": [...]}``."""
        sid = quote(session_id, safe="")
        status, body = await self.http.call("GET", f"/api/sessions/{sid}/messages?" + urlencode(
            {"limit": min(max(int(limit), 1), 500), "inline_images": "false"}))
        if status == 404:
            raise HermletError("no such conversation")
        body = self.http._checked(status, body, "messages")
        messages = [{"id": m.get("id"), "role": m["role"], "content": _plain(m["content"]), "timestamp": m.get("timestamp") or 0}
                    for m in _visible(body.get("messages") or [])]
        try:
            meta = await self.http.get(f"/api/sessions/{sid}")
            meta = meta.get("session") if isinstance(meta.get("session"), dict) else meta
        except HermletError:
            meta = {"id": session_id}
        return {"session": self._row(meta), "messages": messages}

    async def resume(self, session_id: str) -> Dict[str, Any]:
        """Carry on a past conversation on this device.

        One of this device's own conversations is reopened as it is. A
        conversation from another surface (Discord, the CLI) is continued in a
        new hermlet conversation that starts with its history, so the original
        stays exactly as it was where it lives.
        """
        self._require_ready()
        found = await self.session_messages(session_id, limit=SEED_MESSAGES)
        row = found["session"]
        old, self._sid = self._sid, ""
        if old:
            try:
                await self._rpc("session.close", session_id=old)
            except HermletError:
                pass
        if row["source"] == SOURCE:
            result = await self._rpc("session.resume", session_id=session_id, source=SOURCE, omit_messages=True, timeout=60)
            self._sid = str(result.get("session_id") or "")
            self._remember_session(str(result.get("stored_session_id") or session_id), row["title"])
        else:
            seed = [{"role": m["role"], "content": m["content"][:SEED_CHARS]} for m in found["messages"][-SEED_MESSAGES:]]
            created = await self._rpc("session.create", source=SOURCE, parent_session_id=session_id,
                                      messages=await self._preamble() + seed, timeout=60)
            self._sid = str(created["session_id"])
            self._remember_session(str(created.get("stored_session_id") or ""), row["title"])
        await self._emit("session", id=self.stored_session, title=row["title"])
        return {"session": {**row, "id": self.stored_session, "source": SOURCE, "continued_from": session_id,
                            "current": True}}


class Utterance:
    """One spoken message: recorded here, transcribed by Hermes, then sent as text."""

    def __init__(self, device: DashboardDevice, rate: int):
        self._device = device
        self.rate = rate
        self._pcm = bytearray()
        self._open = False

    async def __aenter__(self) -> "Utterance":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            await self.end()
        else:
            await self.discard()

    async def start(self) -> None:
        self._device._require_ready()
        self._open = True

    async def write(self, pcm: bytes) -> None:
        if self._open and len(self._pcm) < self.rate * 2 * 120:
            self._pcm += pcm

    async def discard(self) -> None:
        self._open = False
        self._pcm.clear()

    async def end(self) -> None:
        if not self._open:
            return
        self._open = False
        device = self._device
        if len(self._pcm) < self.rate // 2:  # under a quarter of a second
            await device._emit("notice", text="That was too short to hear", ttl_s=5)
            return
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.rate)
            w.writeframes(bytes(self._pcm))
        await device._emit("status", text="Listening back")
        try:
            heard = await device.transcribe(buf.getvalue())
        except HermletError as exc:
            await device._emit("status", text="")
            await device._emit("notice", text=f"Hermes could not transcribe that: {exc}", ttl_s=20)
            return
        await device._emit("status", text="")
        if not heard:
            await device._emit("notice", text="I did not catch any words", ttl_s=5)
            return
        await device._emit("transcript", text=heard)
        await device.send(heard)


def _visible(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The rows a person said or was told: no tool traffic, and none of the hidden context rows."""
    out, skip_ack = [], False
    for m in rows:
        content = m.get("content")
        if m.get("role") not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
            continue
        if m.get("display_kind") == "hidden" or content.startswith(look_mod.CONTEXT_MARK):
            skip_ack = m.get("role") == "user"
            continue
        if skip_ack and m.get("role") == "assistant" and content.strip() == "Understood.":
            skip_ack = False
            continue
        skip_ack = False
        out.append(m)
    return out


def _plain(content: str) -> str:
    """A stored message without the file contents Hermes inlined for the model."""
    return content.split(_ATTACHED_CONTEXT, 1)[0].strip()


def _tool_phrase(name: str, payload: Dict[str, Any]) -> str:
    preview = str(payload.get("preview") or payload.get("context") or "").strip().splitlines()
    label = name.replace("_", " ").replace(".", " ").strip().capitalize()
    return f"{label}: {preview[0][:60]}" if preview and preview[0] else label
