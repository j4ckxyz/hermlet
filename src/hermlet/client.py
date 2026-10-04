"""``hermlet.connect()``: how an app on this machine talks to Hermes.

The ``hermlet run`` service holds the connection; an app opens the local
socket and shares it. No credentials, no asyncio, nothing to configure::

    import hermlet

    h = hermlet.connect()
    print(h.ask("What is on my calendar today?"))

    h.send("The garage door has been open for an hour.", files=["snapshot.jpg"])

    @h.action("lamp.set", "Turn the desk lamp on or off.",
              {"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]})
    def lamp(on: bool):
        ...
        return {"on": on}

    @h.on("reply")
    def show(msg):
        print(msg["text"])

    h.run_forever()

The same socket is open to any language; see ``docs/local-api.md``.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Union

from . import config
from .base import HermletError

Handler = Callable[[Dict[str, Any]], Any]


class Hermlet:
    def __init__(self, socket_path: Optional[Union[str, Path]] = None, timeout: float = 10):
        self._path = str(socket_path or config.socket_path())
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        try:
            self._sock.connect(self._path)
        except OSError as exc:
            self._sock.close()
            raise HermletError(
                f"hermlet is not running on this machine ({self._path}: {exc.strerror or exc}). "
                "Start it with 'hermlet run' or 'systemctl --user start hermlet'.") from None
        self._sock.settimeout(None)
        self._file = self._sock.makefile("rb")
        self._lock = threading.Lock()
        self._events: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self._pending: Dict[str, "queue.Queue[Dict[str, Any]]"] = {}
        self._listeners: List[Handler] = []
        self._handlers: Dict[str, List[Handler]] = {}
        self._actions: Dict[str, Dict[str, Any]] = {}
        self._timeout = timeout
        self._closed = False
        self.status: Dict[str, Any] = {}
        first = self._file.readline()
        if first:
            self.status = json.loads(first)
        self._thread = threading.Thread(target=self._read, name="hermlet-client", daemon=True)
        self._thread.start()

    def __enter__(self) -> "Hermlet":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()

    # -- plumbing -------------------------------------------------------------------

    def _read(self) -> None:
        try:
            for line in self._file:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("type") == "result" and msg.get("req") in self._pending:
                    self._pending[msg["req"]].put(msg)
                    continue
                self._track(msg)
                for listener in list(self._listeners):
                    listener(msg)
                self._events.put(msg)
        except (OSError, ValueError):
            pass
        finally:
            self._closed = True
            self._events.put(None)
            for waiting in list(self._pending.values()):
                waiting.put({"ok": False, "error": "hermlet stopped"})

    def _track(self, msg: Dict[str, Any]) -> None:
        """Keep ``status`` current, so an app can read it without asking."""
        kind = msg.get("type")
        if kind == "link":
            self.status["state"] = msg.get("state")
        elif kind == "pairing":
            self.status["pairing"] = {"code": msg.get("code"), "command": msg.get("command")}
        elif kind == "persona":
            self.status["persona"] = msg.get("persona")
            self.status["persona_chosen"] = bool((msg.get("persona") or {}).get("chosen"))
        elif kind == "look":
            self.status["look"] = msg.get("look")
        elif kind in ("turn.start", "turn.end"):
            self.status["busy"] = kind == "turn.start"

    def _write(self, msg: Dict[str, Any]) -> None:
        if self._closed:
            raise HermletError("hermlet stopped")
        data = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        with self._lock:
            try:
                self._sock.sendall(data)
            except OSError as exc:
                raise HermletError(f"hermlet stopped ({exc})") from None

    def request(self, op: str, timeout: Optional[float] = None, **fields: Any) -> Dict[str, Any]:
        """Send one op and wait for its result. Raises ``HermletError`` when it fails."""
        req = uuid.uuid4().hex[:10]
        waiting: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._pending[req] = waiting
        try:
            self._write({"op": op, "req": req, **fields})
            try:
                result = waiting.get(timeout=timeout or self._timeout)
            except queue.Empty:
                raise HermletError(f"hermlet did not answer '{op}' in time") from None
        finally:
            self._pending.pop(req, None)
        if not result.get("ok"):
            raise HermletError(str(result.get("error") or f"'{op}' failed"))
        return result

    # -- talking to Hermes ------------------------------------------------------------

    @property
    def state(self) -> str:
        """``offline``, ``connecting``, ``pairing`` (waiting for approval) or ``ready``."""
        return str(self.status.get("state") or "offline")

    def send(self, text: str = "", files: Sequence[Union[str, "os.PathLike[str]"]] = ()) -> str:
        """Send a message, optionally with files or images. Returns the message id."""
        paths = [os.path.abspath(os.path.expanduser(os.fspath(f))) for f in files]
        return str(self.request("send", timeout=180, text=text, files=paths).get("id") or "")

    def ask(self, text: str, files: Sequence[Union[str, "os.PathLike[str]"]] = (), timeout: float = 600) -> str:
        """Send a message and return the agent's answer to it."""
        replies: List[str] = []
        done = threading.Event()

        def collect(msg: Dict[str, Any]) -> None:
            if msg.get("type") == "reply" and not msg.get("interim"):
                replies.append(str(msg.get("text") or ""))
            elif msg.get("type") == "turn.end":
                done.set()

        self._listeners.append(collect)
        try:
            self.send(text, files)
            if not done.wait(timeout):
                raise HermletError("Hermes did not finish answering in time")
            if not replies:
                done.clear()
                done.wait(0.3)
        finally:
            self._listeners.remove(collect)
        return "\n\n".join(replies)

    def cancel(self) -> None:
        """Stop what Hermes is doing."""
        self.request("cancel")

    def new_session(self) -> None:
        """Start a fresh conversation."""
        self.request("new")

    def answer(self, prompt_id: str, answer: str) -> None:
        """Answer a ``prompt`` event (``yes``/``no`` or one of its choices)."""
        self.request("answer", id=prompt_id, answer=answer)

    def talk_start(self) -> None:
        """Start listening on the microphone; ``talk_stop()`` sends what was said."""
        self.request("talk.start")

    def talk_stop(self, send: bool = True) -> None:
        self.request("talk.stop" if send else "talk.cancel")

    def event(self, name: str, data: Any = None, notify: bool = False) -> None:
        """Report that something happened. With ``notify`` the agent is told, as a message."""
        self.request("event", name=name, data=data, notify=notify)

    def report(self, **sensors: Any) -> None:
        """Share readings (``temperature_c=21.5``) the agent can look up."""
        self.request("report", sensors=sensors)

    def sessions(self, limit: int = 20, offset: int = 0, source: Optional[str] = None,
                 query: Optional[str] = None) -> List[Dict[str, Any]]:
        """Past conversations with this agent from every surface: Discord, the web, other hermlets."""
        return list(self.request("sessions.list", timeout=40, limit=limit, offset=offset, source=source,
                                 query=query).get("sessions") or [])

    def session_messages(self, session_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        return list(self.request("sessions.get", timeout=40, session_id=session_id, limit=limit).get("messages") or [])

    def resume(self, session_id: str) -> Dict[str, Any]:
        """Carry on a past conversation on this device."""
        return dict(self.request("sessions.resume", timeout=40, session_id=session_id).get("session") or {})

    def set_persona(self, name: Optional[str] = None, colour: Optional[str] = None) -> Dict[str, Any]:
        """Name and colour the critter (a swatch name like ``"teal"`` or ``"#4fb3a6"``).

        Saved on Hermes: every device of this agent shows it, and the agent's own notes get a
        line about it. Returns ``{"persona": {...}, "memory": "saved" | "full" | "off" | "failed"}``.
        """
        result = self.request("persona.set", timeout=40, name=name, colour=colour)
        return {"persona": result.get("persona"), "memory": result.get("memory")}

    def set_look(self, this_device_only: bool = False, **changes: Any) -> Dict[str, Any]:
        """Change how the app looks: ``h.set_look(accent="red", font="mono", fun=False)``.

        Saved on Hermes, so every device follows (or only this one, with ``this_device_only``).
        The keys are in ``look.py`` and ``docs/local-api.md``. Returns what this device now shows.
        """
        return dict(self.request("look.set", timeout=40, changes=changes, this_device_only=this_device_only).get("look") or {})

    def suggest_name(self) -> str:
        """A name the critter makes up for itself."""
        return str(self.request("persona.suggest").get("name") or "")

    def refresh(self) -> Dict[str, Any]:
        self.status = self.request("status")["status"]
        return self.status

    # -- reacting ---------------------------------------------------------------------

    def events(self, timeout: Optional[float] = None) -> Iterator[Dict[str, Any]]:
        """Every event as it happens. Ends when hermlet stops (or after ``timeout`` idle seconds)."""
        while True:
            try:
                msg = self._events.get(timeout=timeout)
            except queue.Empty:
                return
            if msg is None:
                return
            yield msg

    def on(self, type_: str) -> Callable[[Handler], Handler]:
        """Call a function for each event of this type (``"*"`` for all) during ``run_forever()``."""
        def register(fn: Handler) -> Handler:
            self._handlers.setdefault(type_, []).append(fn)
            return fn
        return register

    def action(self, name: str, description: str = "", params: Optional[Dict[str, Any]] = None):
        """Offer something the agent can do here. The function gets the arguments as keywords."""
        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._actions[name] = {"fn": fn, "description": description or (fn.__doc__ or "").strip(),
                                   "params": params or {"type": "object", "properties": {}}}
            self._write({"op": "actions", "actions": [
                {"name": n, "description": a["description"], "params": a["params"]} for n, a in self._actions.items()]})
            return fn
        return register

    def run_forever(self) -> None:
        """Serve handlers and actions until hermlet stops."""
        for msg in self.events():
            if msg.get("type") == "action":
                self._run_action(msg)
            for fn in self._handlers.get(str(msg.get("type")), []) + self._handlers.get("*", []):
                fn(msg)

    def _run_action(self, msg: Dict[str, Any]) -> None:
        entry = self._actions.get(str(msg.get("name")))
        try:
            if entry is None:
                raise LookupError(f"no action named {msg.get('name')!r}")
            result = entry["fn"](**(msg.get("args") or {}))
            json.dumps(result)
            self._write({"op": "action.result", "id": msg.get("id"), "ok": True, "result": result})
        except Exception as exc:
            self._write({"op": "action.result", "id": msg.get("id"), "ok": False,
                         "error": str(exc) or exc.__class__.__name__})


def connect(socket_path: Optional[Union[str, Path]] = None, timeout: float = 10) -> Hermlet:
    """Open the local hermlet service."""
    return Hermlet(socket_path, timeout)
