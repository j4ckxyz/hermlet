"""``hermlet run``: the service that keeps this machine connected to Hermes.

It owns the one connection (a ``Device``), the microphone and the speaker,
and shares them with every program on the machine through a local socket:
the native app, ``hermlet send``, and anything you write with
``hermlet.connect()``. The socket speaks newline-delimited JSON; the contract
is in ``docs/local-api.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from . import audio, builtin, config, persona as persona_mod
from .base import READY, BaseDevice, HermletError, Unconfigured

log = logging.getLogger("hermlet.service")

CHAT_KEEP = 200
IDLE_CHECK_S = 20
SOCKET_LIMIT = 4 * 1024 * 1024


class _Client:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader, self.writer = reader, writer
        self.actions: Set[str] = set()
        self.pending: Dict[str, asyncio.Future] = {}

    def send(self, msg: Dict[str, Any]) -> None:
        if self.writer.is_closing():
            return
        try:
            self.writer.write(json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode() + b"\n")
        except (OSError, RuntimeError):
            pass


class Service:
    def __init__(self, home: Optional[Path] = None, url: Optional[str] = None, name: Optional[str] = None):
        self.home = Path(home) if home else config.home()
        self._overrides = (url, name)
        self.clients: Set[_Client] = set()
        self.chat: List[Dict[str, Any]] = []
        self.busy = False
        self.status_text = ""
        self.prompt: Optional[Dict[str, Any]] = None
        self.session_title = ""
        self._recorder: Optional[audio.Recorder] = None
        self._talk_task: Optional[asyncio.Task] = None
        self._server: Optional[asyncio.AbstractServer] = None
        self._history_loaded = False
        self._last_activity = time.time()
        self._idle_task: Optional[asyncio.Task] = None
        self._stopping = False
        self.link_error = ""  # why Hermes cannot be reached, for an app that opens later
        self.device: BaseDevice = self._make_device(url, name)

    def _make_device(self, url: Optional[str] = None, name: Optional[str] = None) -> BaseDevice:
        """The connection for the address in the settings, or a placeholder when there is none yet."""
        from . import Device

        cfg = config.load(self.home)
        if not (url or cfg.get("url")):
            device: BaseDevice = Unconfigured(self.home)
        else:
            device = Device(url, name, home=self.home, mic=audio.can_record(), speaker=audio.can_play())
            builtin.register(device)
        device.on("*")(self._on_device)
        return device

    # -- lifecycle ------------------------------------------------------------------

    async def run(self) -> None:
        await self.start()
        try:
            while not self._stopping:
                device = self.device
                await device.run()
                if self.device is device:
                    break  # it was stopped for good, not swapped for a new address
        finally:
            await self.stop()

    async def configure(self, url: str, name: Optional[str] = None, mode: Optional[str] = None) -> None:
        """Point this machine at a Hermes (first run, or a corrected address) and start connecting."""
        from . import mode_for, protocol
        from .dashboard import normalize_base

        url = str(url or "").strip()
        picked = mode_for(url, mode or "")
        try:  # say what is wrong with an address before saving it
            if picked == "plugin":
                url = protocol.normalize_url(url)
            elif "://" in url:
                url = normalize_base(url)
            else:
                # Just a name was typed. Try the usual places a dashboard answers and keep the one that does.
                guesses = [normalize_base("https://" + url), normalize_base("http://" + url)]
                url = await _first_answering(guesses) or guesses[0 if url.endswith(".ts.net") else 1]
        except ValueError as exc:
            raise HermletError(str(exc)) from None
        saved = config.read_json(self.home / "config.json", {}) or {}
        was = saved.get("url")
        same = was == url and (not name or saved.get("name") == name) and not isinstance(self.device, Unconfigured)
        saved.update({"url": url, "mode": picked})
        if name:
            saved["name"] = str(name).strip()[:64]
        config.save(saved, self.home)
        if same:
            self.device.retry_now()
            return
        await self._stop_talking(send=False)
        if saved.get("url") != was:
            # Another Hermes has another critter and another look; do not show the last one's while connecting.
            for cached in ("persona.json", "look.json"):
                with contextlib.suppress(OSError):
                    (self.home / cached).unlink()
        old, self.device = self.device, self._make_device()
        self.chat, self.session_title, self.busy, self.status_text, self.prompt = [], "", False, "", None
        self._history_loaded, self.link_error = False, ""
        await old.stop()  # run() notices the swap and starts the new connection
        self.broadcast(self.snapshot())

    async def start(self) -> None:
        path = config.socket_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if await _socket_alive(path):
                raise HermletError(f"hermlet is already running here ({path})")
            path.unlink()
        self._server = await asyncio.start_unix_server(self._serve, path=str(path), limit=SOCKET_LIMIT)
        self._idle_task = asyncio.ensure_future(self._watch_idle())
        os.chmod(path, 0o600)
        log.info("local socket at %s", path)

    async def _watch_idle(self) -> None:
        """Come back to a fresh conversation after being left alone for a while.

        A long conversation is re-sent to the model on every turn, so letting one run for days
        costs more and more. The old conversation stays in the history, one tap away.
        """
        while True:
            await asyncio.sleep(IDLE_CHECK_S)
            try:
                await self._freshen()
            except (HermletError, asyncio.TimeoutError) as exc:
                log.debug("could not start a fresh conversation: %s", exc)

    async def _freshen(self) -> bool:
        minutes = self.device.look.get("fresh_after_min") or 0
        idle = time.time() - self._last_activity
        if not minutes or idle < minutes * 60 or self.busy or self._recorder is not None:
            return False
        if not self.chat or self.device.state != READY:
            return False
        log.info("idle for %d minutes: starting a fresh conversation", idle // 60)
        await self._new_conversation()
        return True

    async def _new_conversation(self) -> None:
        await self.device.new_session()
        self.chat, self.session_title = [], ""
        self._last_activity = time.time()
        self.broadcast({"type": "chat", "chat": [], "session_title": ""})

    async def stop(self) -> None:
        self._stopping = True
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None
        await self._stop_talking(send=False)
        await self.device.stop()
        if self._server is not None:
            self._server.close()
            self._server = None
        for client in list(self.clients):
            client.writer.close()
        try:
            config.socket_path(self.home).unlink()
        except OSError:
            pass

    # -- what apps see ----------------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        d = self.device
        return {
            "type": "hello", "version": 1, "mode": d.mode,
            "device": {"id": d.device_id, "name": d.name, "kind": d.kind},
            "url": d.url, "state": d.state, "error": self.link_error, "agent": d.agent, "persona": d.persona, "pairing": d.pairing,
            "persona_chosen": d.persona_chosen, "look": d.look,
            "swatches": [{"name": name, "hex": value} for name, value in persona_mod.COLOURS],
            "chat": self.chat[-CHAT_KEEP:], "busy": self.busy, "status": self.status_text, "prompt": self.prompt,
            "session_title": self.session_title,
            "mic": {"available": "mic" in d.caps, "recording": self._recorder is not None},
            "speaker": audio.can_play() or audio.can_play_files(),
            "speak": bool(d.look.get("speak")), "theme": d.look.get("theme") or "auto",
            "inbox": str(config.inbox(d.cfg)),
        }

    def broadcast(self, msg: Dict[str, Any]) -> None:
        for client in list(self.clients):
            client.send(msg)

    def _say(self, role: str, text: str, **extra: Any) -> None:
        entry = {"role": role, "text": text, "ts": time.time(), **{k: v for k, v in extra.items() if v}}
        self.chat.append(entry)
        del self.chat[:-CHAT_KEEP]

    async def _on_device(self, msg: Dict[str, Any]) -> None:
        kind = msg["type"]
        if kind in ("turn.start", "turn.end", "reply", "transcript"):
            self._last_activity = time.time()
        if kind == "turn.start":
            self.busy = True
        elif kind == "turn.end":
            self.busy, self.status_text = False, ""
        elif kind == "status":
            self.status_text = str(msg.get("text") or "")
        elif kind == "reply":
            self._say("assistant", str(msg.get("text") or ""), interim=bool(msg.get("interim")))
        elif kind == "transcript":
            self._say("user", str(msg.get("text") or ""), via="voice")
        elif kind == "file":
            self._say("assistant", str(msg.get("caption") or ""), files=[{"name": msg["name"], "path": msg["path"], "mime": msg["mime"]}])
        elif kind == "session":
            self.session_title = str(msg.get("title") or "")
            if not msg.get("id"):
                self.chat = []
        elif kind == "prompt":
            self.prompt = msg
        elif kind == "prompt.close":
            if self.prompt and self.prompt.get("id") == msg.get("id"):
                self.prompt = None
        elif kind == "link":
            self.link_error = str(msg.get("error") or "")
            if msg.get("state") != READY:
                self.busy, self.status_text = False, ""
                await self._stop_talking(send=False)
            elif not self._history_loaded:
                asyncio.ensure_future(self._load_history())
        self.broadcast(msg)

    async def _load_history(self) -> None:
        """Show the conversation this device was already having, after a restart."""
        self._history_loaded = True
        if self.chat:
            return
        try:
            current_id = getattr(self.device, "stored_session", "")
            if not current_id:
                current = next((s for s in await self.device.sessions(limit=30) if s.get("current")), None)
                if current is None:
                    return
                current_id = current["id"]
            result = await self.device.session_messages(current_id, limit=CHAT_KEEP)
        except (HermletError, asyncio.TimeoutError) as exc:
            log.debug("could not load the current conversation: %s", exc)
            return
        self._adopt(result)

    def _adopt(self, result: Dict[str, Any]) -> None:
        self.chat = [{"role": m["role"], "text": m.get("content") or "", "ts": m.get("timestamp") or 0}
                     for m in result.get("messages") or [] if m.get("role") in ("user", "assistant") and m.get("content")]
        self.session_title = str((result.get("session") or {}).get("title") or "")
        self.broadcast({"type": "chat", "chat": self.chat, "session_title": self.session_title})

    # -- voice ------------------------------------------------------------------------

    async def _start_talking(self) -> None:
        if self._recorder is not None:
            return
        cfg = self.device.cfg
        recorder = audio.Recorder(int(cfg.get("mic_rate") or 16000), cfg.get("mic") or "")
        if not recorder.available:
            raise HermletError("no microphone tool found; install alsa-utils")
        utterance = self.device.voice(recorder.rate)
        await recorder.start()
        await utterance.start()
        self._recorder = recorder
        self._utterance = utterance
        self._talk_task = asyncio.ensure_future(self._pump_mic(recorder, utterance))
        self.broadcast({"type": "mic", "state": "recording", "level": 0})

    async def _pump_mic(self, recorder: audio.Recorder, utterance) -> None:
        limit = float(self.device.limits.get("utterance_s") or 120)
        started = time.monotonic()
        heard = 0
        try:
            async for chunk in recorder.chunks():
                heard += len(chunk)
                await utterance.write(chunk)
                self.broadcast({"type": "mic", "state": "recording", "level": round(audio.level(chunk), 3)})
                if time.monotonic() - started > limit:
                    asyncio.ensure_future(self._stop_talking(send=True))
                    return
        except HermletError:
            pass
        if self._recorder is recorder and not heard:
            # The recorder gave up at once: no microphone, or it is in use. Say so instead of "listening".
            asyncio.ensure_future(self._stop_talking(send=False))
            self.broadcast({"type": "notice", "text": "No sound is coming from the microphone. Is one plugged in?", "ttl_s": 8})

    async def _stop_talking(self, send: bool) -> None:
        recorder, self._recorder = self._recorder, None
        if recorder is None:
            return
        await recorder.stop()
        task, self._talk_task = self._talk_task, None
        if task is not None and task is not asyncio.current_task():
            try:
                await asyncio.wait_for(task, 2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        try:
            if send:
                await self._utterance.end()
            else:
                await self._utterance.discard()
        except HermletError:
            pass
        self.broadcast({"type": "mic", "state": "idle", "level": 0})

    # -- the local socket ---------------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = _Client(reader, writer)
        self.clients.add(client)
        client.send(self.snapshot())
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    asyncio.ensure_future(self._handle(client, msg))
        except (ConnectionError, asyncio.IncompleteReadError, ValueError):
            pass
        finally:
            self.clients.discard(client)
            for name in client.actions:
                self.device.remove_action(name)
            for future in client.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("the app providing this action went away"))
            writer.close()

    async def _handle(self, client: _Client, msg: Dict[str, Any]) -> None:
        op, req = str(msg.get("op") or ""), msg.get("req")
        try:
            result = await self._op(client, op, msg)
        except Exception as exc:  # an app must always get an answer, never a silent hang
            if not isinstance(exc, (HermletError, asyncio.TimeoutError, OSError, ValueError, KeyError, TypeError)):
                log.exception("op %s failed", op)
            if op != "action.result":
                client.send({"type": "result", "req": req, "op": op, "ok": False,
                             "error": str(exc) or exc.__class__.__name__})
            return
        if req is not None:
            client.send({"type": "result", "req": req, "op": op, "ok": True, **(result or {})})

    async def _op(self, client: _Client, op: str, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        d = self.device
        if op == "status":
            return {"status": self.snapshot()}
        if op == "setup":
            await self.configure(str(msg.get("url") or ""), msg.get("name") or None, msg.get("mode") or None)
            return None
        if op == "look.set":
            changes = msg.get("changes") if isinstance(msg.get("changes"), dict) else {}
            if not changes:
                raise HermletError("nothing to change; see docs/local-api.md for the keys")
            return {"look": await d.set_look(changes, bool(msg.get("this_device_only")))}
        if op == "look.teach":
            return {"taught": await d.teach_agent()}
        if op == "persona.suggest":
            return {"name": persona_mod.suggest_name()}
        if op == "persona.preview":
            return {"persona": d.draw_persona(msg.get("name"), msg.get("colour"))}
        if op == "persona.set":
            if msg.get("colour") and not persona_mod.colour_hex(msg.get("colour")):
                raise HermletError("that is not a colour; use one of the swatches or a value like #4fb3a6")
            result = await d.set_persona(msg.get("name"), msg.get("colour"))
            result["taught"] = await d.teach_agent()  # naming the critter is also when Hermes learns to restyle it
            return result
        if op == "signin":
            if not hasattr(d, "sign_in"):
                raise HermletError("this device pairs with a code, not a password")
            await d.sign_in(str(msg.get("username") or ""), str(msg.get("password") or ""))
            return None
        if op == "signout":
            if hasattr(d, "sign_out"):
                await d.sign_out()
            return None
        if op in ("send", "talk.start"):
            await self._freshen()  # back after a long while: this message opens a new conversation
            self._last_activity = time.time()
        if op == "send":
            text = str(msg.get("text") or "")
            files = [str(f) for f in msg.get("files") or []]
            for f in files:
                if not Path(f).expanduser().is_file():
                    raise HermletError(f"no such file: {f}")
            if not text.strip() and not files:
                raise HermletError("nothing to send")
            if files:
                self.broadcast({"type": "uploading", "files": [Path(f).name for f in files]})
            msg_id = await d.send(text, files)
            entry_files = [{"name": Path(f).name, "path": str(Path(f).expanduser())} for f in files]
            self._say("user", text, files=entry_files)
            self.broadcast({"type": "user", "id": msg_id, "text": text, "files": entry_files, "ts": time.time()})
            return {"id": msg_id}
        if op == "talk.start":
            await self._start_talking()
        elif op == "talk.stop":
            await self._stop_talking(send=True)
        elif op == "talk.cancel":
            await self._stop_talking(send=False)
        elif op == "cancel":
            await d.cancel()
        elif op == "new":
            await self._new_conversation()
        elif op == "answer":
            await d.answer(str(msg["id"]), str(msg.get("answer") or ""))
            if self.prompt and self.prompt.get("id") == msg["id"]:
                self.prompt = None
            self.broadcast({"type": "prompt.close", "id": msg["id"]})
        elif op == "sessions.list":
            return {"sessions": await d.sessions(int(msg.get("limit") or 20), int(msg.get("offset") or 0),
                                                 msg.get("source") or None, msg.get("query") or None)}
        elif op == "sessions.get":
            result = await d.session_messages(str(msg["session_id"]), int(msg.get("limit") or 50), msg.get("before"))
            return {"session": result.get("session"), "messages": result.get("messages") or []}
        elif op == "sessions.resume":
            self._last_activity = time.time()
            result = await d.resume(str(msg["session_id"]))
            try:
                self._adopt(await d.session_messages(str(result.get("session", {}).get("id") or msg["session_id"]),
                                                     limit=CHAT_KEEP))
            except (HermletError, asyncio.TimeoutError):
                pass
            return {"session": result.get("session")}
        elif op == "actions":
            self._offer(client, msg.get("actions") or [])
        elif op == "action.result":
            future = client.pending.pop(str(msg.get("id")), None)
            if future is not None and not future.done():
                if msg.get("ok"):
                    future.set_result(msg.get("result"))
                else:
                    future.set_exception(RuntimeError(str(msg.get("error") or "the action failed")))
        elif op == "event":
            await d.event(str(msg["name"]), msg.get("data"), bool(msg.get("notify")))
        elif op == "report":
            await d.report(**(msg.get("sensors") or {}))
        elif op == "set":
            await self._set(msg)
        else:
            raise HermletError(f"unknown op: {op or '(none)'}")
        return None

    def _offer(self, client: _Client, actions: List[Dict[str, Any]]) -> None:
        for name in client.actions:
            self.device.remove_action(name)
        client.actions.clear()
        for action in actions:
            name = str(action.get("name") or "")
            if not name:
                continue

            async def call(_name: str = name, **args: Any) -> Any:
                call_id = uuid.uuid4().hex[:8]
                future: asyncio.Future = asyncio.get_running_loop().create_future()
                client.pending[call_id] = future
                client.send({"type": "action", "id": call_id, "name": _name, "args": args})
                try:
                    return await asyncio.wait_for(future, 18)
                finally:
                    client.pending.pop(call_id, None)

            self.device.add_action(name, call, str(action.get("description") or ""), action.get("params"))
            client.actions.add(name)

    async def _set(self, msg: Dict[str, Any]) -> None:
        """The older, shorter way to change the theme or spoken replies; the same as ``look.set``."""
        changed: Dict[str, Any] = {}
        if "theme" in msg:
            if msg["theme"] not in ("auto", "light", "dark"):
                raise HermletError("theme is auto, light or dark")
            changed["theme"] = msg["theme"]
        if "speak" in msg:
            changed["speak"] = bool(msg["speak"])
        if not changed:
            raise HermletError("nothing to change; settable: theme, speak")
        await self.device.set_look(changed)


def _answers(url: str) -> bool:
    """Whether a Hermes dashboard answers at this address."""
    import urllib.error
    import urllib.request

    from . import net

    net.install()
    try:
        with urllib.request.urlopen(url + "/api/status", timeout=5) as response:
            return isinstance(json.loads(response.read() or b"null"), dict)
    except urllib.error.HTTPError as exc:
        return exc.code in (401, 403)  # it is there, and wants a sign-in first
    except Exception:
        return False


async def _first_answering(urls: List[str]) -> Optional[str]:
    loop = asyncio.get_running_loop()
    for url in urls:
        if await loop.run_in_executor(None, _answers, url):
            return url
    return None


async def _socket_alive(path: Path) -> bool:
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path)), 1)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    return True
