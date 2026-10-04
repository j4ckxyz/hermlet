"""What every hermlet connection has in common, whichever way it reaches Hermes.

A device registers handlers and actions, runs a reconnecting loop, and turns
whatever its backend speaks into one small vocabulary of events:

    link          {"state": "setup" | "offline" | "connecting" | "signin" | "pairing" | "ready"}
    turn.start    Hermes began working on a message
    status        {"text": "Searching the web"}      (empty text clears it)
    tool          {"name": "web_search", "state": "start" | "done"}
    reply.delta   {"text": "the reply so far"}       (cumulative)
    reply         {"text": "...", "interim": bool}
    turn.end      {"outcome": "success" | "failure" | "cancelled"}
    transcript    {"text": "what speech-to-text heard"}
    notice        {"text": "...", "ttl_s": 8}
    card          {"title": "...", "body": "...", "ttl_s": 20}
    prompt        {"id": "...", "title": "...", "text": "...", "choices": [...]}
    prompt.close  {"id": "..."}
    file          {"path": "...", "name": "...", "mime": "...", "caption": "..."}
    session       {"id": "...", "title": "..."}
    persona       {"persona": {...}}
    look          {"look": {...}}                    (how the app should look; see look.py)

Backends: ``dashboard.DashboardDevice`` (Hermes's own web API, nothing to
install on the Hermes side) and ``device.HubDevice`` (the optional plugin).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import platform
import random
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple, Union

from . import config, look as look_mod, net, persona as persona_mod
from .audio import Player

log = logging.getLogger("hermlet")

Handler = Callable[[Dict[str, Any]], Union[None, Awaitable[None]]]
FileRef = Union[str, "os.PathLike[str]", Tuple[str, bytes]]

OFFLINE, CONNECTING, SIGNIN, PAIRING, READY = "offline", "connecting", "signin", "pairing", "ready"
SETUP = "setup"  # no Hermes address yet: the app asks for one


class HermletError(RuntimeError):
    pass


class NeedsSignIn(HermletError):
    """The Hermes dashboard wants a username and password before going further."""


class BaseDevice:
    mode = ""

    def __init__(self, name: Optional[str] = None, *, home: Optional[Union[str, Path]] = None,
                 mic: bool = False, speaker: bool = False, markdown: bool = False,
                 caps: Optional[Dict[str, Any]] = None, kind: Optional[str] = None):
        net.install()  # a Tailscale name resolves even where MagicDNS is turned off
        self.home = Path(home) if home else config.home()
        self.cfg = config.load(self.home)
        self.name = name or self.cfg["name"]
        self.kind = kind or f"{platform.system().lower()}-{platform.machine()}"
        self.caps: Dict[str, Any] = {"text": {"markdown": markdown}, "files": True, "sessions": True}
        if mic:
            self.caps["mic"] = {"rate": int(self.cfg.get("mic_rate") or 16000), "format": "pcm16"}
        if speaker:
            self.caps["speaker"] = {"format": "pcm16"}
        self.caps.update(caps or {})
        self.state = OFFLINE
        self.persona: Dict[str, Any] = config.read_json(self.home / "persona.json", {}) or {}
        self.look_file: Dict[str, Any] = look_mod.parse(_read_text(self.home / "look.json"))
        self.look: Dict[str, Any] = look_mod.resolve(self.look_file, self.name)
        self.cfg["speak"] = bool(self.look["speak"])
        self.agent = "Hermes"
        self.pairing: Optional[Dict[str, str]] = None
        self.limits: Dict[str, Any] = {}
        self.sensors: Dict[str, Any] = {}
        self.player = Player(self.cfg.get("speaker") or "")
        self._actions: Dict[str, Dict[str, Any]] = {}
        self._handlers: Dict[str, List[Handler]] = {}
        self._stopping = False
        self._ready = asyncio.Event()
        self._wake = asyncio.Event()

    @property
    def device_id(self) -> str:
        raise NotImplementedError

    # -- registering behaviour ---------------------------------------------------

    def on(self, type_: str) -> Callable[[Handler], Handler]:
        """Call a function for every event of this type (``"*"`` for all)."""
        def register(fn: Handler) -> Handler:
            self._handlers.setdefault(type_, []).append(fn)
            return fn
        return register

    def action(self, name: str, description: str = "", params: Optional[Dict[str, Any]] = None):
        """Offer something the agent can do on this device.

        ``params`` is a JSON Schema object; its properties are passed to the
        function as keyword arguments. Return a JSON-serialisable result, or
        raise to report a failure.
        """
        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.add_action(name, fn, description or (fn.__doc__ or "").strip(), params)
            return fn
        return register

    def add_action(self, name: str, fn: Callable[..., Any], description: str = "",
                   params: Optional[Dict[str, Any]] = None) -> None:
        self._actions[name] = {"fn": fn, "description": description,
                               "params": params or {"type": "object", "properties": {}}}
        self._actions_changed()

    def remove_action(self, name: str) -> None:
        if self._actions.pop(name, None) is not None:
            self._actions_changed()

    def _manifest(self) -> List[Dict[str, Any]]:
        return [{"name": n, "description": a["description"], "params": a["params"]} for n, a in self._actions.items()]

    def _actions_changed(self) -> None:
        pass

    async def run_action(self, name: str, args: Optional[Dict[str, Any]] = None) -> Any:
        entry = self._actions.get(name)
        if entry is None:
            raise LookupError(f"no action named {name!r}")
        result = entry["fn"](**(args or {}))
        if inspect.isawaitable(result):
            result = await result
        json.dumps(result)
        return result

    # -- lifecycle ------------------------------------------------------------------

    async def run(self) -> None:
        """Stay connected until ``stop()``; reconnects with a growing, jittered delay."""
        delay = 1.0
        self._stopping = False
        while not self._stopping:
            try:
                await self._session()
                delay = 1.0
            except NeedsSignIn as exc:
                await self._set_state(SIGNIN, error=str(exc))
                delay = 60.0
            except HermletError as exc:
                log.error("%s", exc)
                await self._set_state(OFFLINE, error=str(exc))
                delay = max(delay, 15.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.info("connection lost: %s", exc or exc.__class__.__name__)
                await self._set_state(OFFLINE, error=str(exc) or exc.__class__.__name__)
            if self._stopping:
                break
            self._wake.clear()
            try:  # sleep, but wake at once when asked (a fresh sign-in, a settings change)
                await asyncio.wait_for(self._wake.wait(), delay + random.uniform(0, delay / 2))
                delay = 1.0
            except asyncio.TimeoutError:
                delay = min(delay * 2, 30.0) if self.state != SIGNIN else 60.0
        await self._set_state(OFFLINE)

    def retry_now(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        await self._close()
        await self.player.abort()

    async def _session(self) -> None:
        raise NotImplementedError

    async def _close(self) -> None:
        raise NotImplementedError

    async def wait_ready(self, timeout: Optional[float] = None) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout)

    async def _set_state(self, state: str, **extra: Any) -> None:
        changed = state != self.state
        self.state = state
        if state == READY:
            self._ready.set()
        else:
            self._ready.clear()
        if changed or extra:
            await self._dispatch({"type": "link", "state": state, **extra})

    def _remember_persona(self, persona: Any) -> bool:
        if not (isinstance(persona, dict) and persona.get("sprite")) or persona == self.persona:
            return False
        self.persona = persona
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            (self.home / "persona.json").write_text(json.dumps(persona), encoding="utf-8")
        except OSError:
            pass
        return True

    # -- the critter ------------------------------------------------------------------

    @property
    def persona_chosen(self) -> bool:
        """Whether the owner has named and coloured the critter (on any device of this Hermes)."""
        return bool(self.persona.get("chosen"))

    def draw_persona(self, name: Optional[str] = None, colour: Optional[str] = None, chosen: bool = False) -> Dict[str, Any]:
        """The critter as it would look with this name and colour. Nothing is saved."""
        current = self.persona or {}
        return persona_mod.generate(
            str(current.get("seed") or self.name), name=persona_mod.clean_name(name) or current.get("name"),
            species=current.get("species"), colour=persona_mod.colour_hex(colour) or current.get("colour"), chosen=chosen)

    async def set_persona(self, name: Optional[str] = None, colour: Optional[str] = None) -> Dict[str, Any]:
        """Name and colour the critter. Saved on Hermes, so every device shows it and the agent knows.

        Returns ``{"persona": {...}, "memory": "saved" | "full" | "off" | "failed"}``; ``memory`` says
        whether the agent's own notes now mention the critter.
        """
        raise NotImplementedError

    # -- the look: shared settings kept on Hermes ---------------------------------------

    async def _take_look(self, raw: Any) -> bool:
        """Adopt a look file from Hermes. Returns whether what this device shows changed."""
        cleaned = look_mod.clean(raw) if isinstance(raw, dict) else look_mod.parse(raw)
        effective = look_mod.resolve(cleaned, self.name)
        changed = effective != self.look
        self.look_file, self.look = cleaned, effective
        self.cfg["speak"] = bool(effective["speak"])
        if changed:
            try:
                self.home.mkdir(parents=True, exist_ok=True)
                (self.home / "look.json").write_text(look_mod.dumps(cleaned), encoding="utf-8")
            except OSError:
                pass
            await self._emit("look", look=effective)
        return changed

    async def set_look(self, changes: Dict[str, Any], this_device_only: bool = False) -> Dict[str, Any]:
        """Change how the app looks (see ``look.py`` for the keys), for every device or just this one.

        Saved on Hermes; returns what this device now shows.
        """
        raise NotImplementedError

    async def teach_agent(self) -> bool:
        """Let the agent restyle the app when asked ("make the accent red"). Returns whether it can now."""
        return False

    async def _dispatch(self, msg: Dict[str, Any]) -> None:
        for fn in self._handlers.get(msg["type"], []) + self._handlers.get("*", []):
            try:
                result = fn(msg)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                log.exception("handler for %s failed", msg["type"])

    async def _emit(self, type_: str, **fields: Any) -> None:
        await self._dispatch({"type": type_, **{k: v for k, v in fields.items() if v is not None}})

    def _require_ready(self) -> None:
        if self.state == READY:
            return
        raise HermletError({
            PAIRING: "this device is not approved by Hermes yet",
            SIGNIN: "sign in to Hermes first (hermlet setup)",
        }.get(self.state, "not connected to Hermes"))

    async def _save_file(self, name: str, data: bytes, mime: str = "", caption: str = "", turn: Any = None) -> None:
        path = _unique(config.inbox(self.cfg) / _safe_name(name))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        await self._dispatch({"type": "file", "path": str(path), "name": path.name, "mime": mime,
                              "caption": caption, "turn": turn, "size": len(data)})

    # -- talking (backends implement send and friends) ----------------------------------

    async def send(self, text: str = "", attachments: Sequence[FileRef] = ()) -> str:
        raise NotImplementedError

    async def ask(self, text: str, attachments: Sequence[FileRef] = (), timeout: float = 600) -> str:
        """Send a message and wait for the answer to that turn."""
        replies: List[str] = []
        done = asyncio.Event()

        def collect(msg: Dict[str, Any]) -> None:
            if msg["type"] == "reply" and not msg.get("interim"):
                replies.append(str(msg.get("text") or ""))
            elif msg["type"] == "turn.end":
                done.set()

        self._handlers.setdefault("*", []).append(collect)
        try:
            await self.send(text, attachments)
            await asyncio.wait_for(done.wait(), timeout)
            if not replies:  # the reply can trail the end of the turn by a moment
                await asyncio.sleep(0.3)
        finally:
            self._handlers["*"].remove(collect)
        return "\n\n".join(replies)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


class Unconfigured(BaseDevice):
    """What the service holds before anyone has said where Hermes is. It only waits."""

    mode = "setup"
    url = ""

    def __init__(self, home: Optional[Union[str, Path]] = None):
        super().__init__(None, home=home)
        self._stopped = asyncio.Event()
        if not self.persona:  # a face for the first screen; the real one comes from Hermes once it is found
            self.persona = persona_mod.generate("hermlet", name="Hermlet")

    @property
    def device_id(self) -> str:
        return "unconfigured"

    async def run(self) -> None:
        await self._set_state(SETUP)
        await self._stopped.wait()

    async def stop(self) -> None:
        self._stopped.set()

    async def send(self, text: str = "", attachments: Sequence[FileRef] = ()) -> str:
        raise HermletError("this device does not know where your Hermes is yet")


async def read_ref(ref: FileRef) -> Tuple[str, bytes]:
    """A file reference (a path, or a ``(name, bytes)`` pair) as its name and bytes."""
    if isinstance(ref, tuple):
        return ref
    path = Path(os.fspath(ref)).expanduser()
    return path.name, await asyncio.get_running_loop().run_in_executor(None, path.read_bytes)


def _safe_name(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "._- " else "_" for c in os.path.basename(name)).strip(" .")
    return cleaned[:120] or "file"


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(2, 1000):
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem}-{uuid.uuid4().hex[:6]}{path.suffix}")
