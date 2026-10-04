"""Agent tools for hermlets: see which devices are around, call what they offer, show a card.

Schemas are static (prompt caching); what a device can do is discovered at run
time through ``hermlet_devices`` and the per-device context Hermes receives
with each message. The tools work in any session served by the gateway process
that hosts the hub, so a device can be driven from another chat ("turn the
desk lamp on" from Discord).
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

from . import runtime, textfmt

TOOLSET = "hermlet"
CALL_TIMEOUT_S = 22.0

_DEVICE_PARAM = {
    "type": "string",
    "description": "Device name or id. Omit to use the hermlet the user is talking through "
                   "(or the only one connected).",
}

DEVICES_SCHEMA = {
    "name": "hermlet_devices",
    "description": "List the user's hermlets (small personal devices such as Raspberry Pis) with the "
                   "actions each one offers and its latest sensor readings.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

ACTION_SCHEMA = {
    "name": "hermlet_action",
    "description": "Call an action a hermlet offers (a lamp, a relay, a camera, a sensor read). "
                   "Use hermlet_devices first if you do not know the action names and parameters.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "Action name as listed by the device."},
            "args": {"type": "object", "description": "Arguments matching the action's parameter schema."},
            "device": _DEVICE_PARAM,
        },
        "required": ["action"],
    },
}

SHOW_SCHEMA = {
    "name": "hermlet_show",
    "description": "Put a short card (a title and a few lines) on a hermlet's screen: a timer, a list, a reminder.",
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Body text to show. Plain text."},
            "title": {"type": "string", "description": "Optional short title."},
            "seconds": {"type": "integer", "description": "How long to show it. Default 20."},
            "device": _DEVICE_PARAM,
        },
        "required": ["text"],
    },
}


def _error(message: str) -> str:
    return json.dumps({"success": False, "error": message})


def _adapter():
    from gateway.session_context import get_session_env

    return runtime.active(get_session_env("HERMES_SESSION_PROFILE") or None)


def _run(adapter, coro) -> Any:
    loop = adapter.loop
    if loop is None or loop.is_closed():
        coro.close()
        raise RuntimeError("the hermlet hub is not running")
    if getattr(loop, "_thread_id", None) == threading.get_ident():
        coro.close()
        raise RuntimeError("hermlet tools cannot block the gateway event loop")
    return asyncio.run_coroutine_threadsafe(coro, loop).result(CALL_TIMEOUT_S)


def _resolve(adapter, ref: str | None):
    from gateway.session_context import get_session_env

    hub = adapter.hub
    if hub is None:
        return None, "the hermlet hub is not running"
    if ref:
        session = hub.find(ref)
        if session is None:
            known = ", ".join(f"{s.name} ({s.device_id})" for s in hub.sessions.values()) or "none connected"
            return None, f"no connected hermlet matches {ref!r}; connected: {known}"
        return session, None
    if get_session_env("HERMES_SESSION_PLATFORM") == "hermlet":
        session = hub.get(get_session_env("HERMES_SESSION_CHAT_ID"))
        if session is not None:
            return session, None
    sessions = list(hub.sessions.values())
    if len(sessions) == 1:
        return sessions[0], None
    if not sessions:
        return None, "no hermlet is connected"
    return None, "several hermlets are connected; pass device: " + ", ".join(s.name for s in sessions)


def _target(args: dict):
    adapter = _adapter()
    if adapter is None:
        return None, None, "the hermlet hub is not running in this process"
    session, err = _resolve(adapter, args.get("device"))
    if err:
        return None, None, err
    if not session.paired:
        return None, None, f"{session.name} is waiting for pairing approval"
    return adapter, session, None


def hermlet_devices(args: dict, **_kw) -> str:
    adapter = _adapter()
    if adapter is None or adapter.hub is None:
        return _error("the hermlet hub is not running in this process")
    online = {s.device_id: s.describe() for s in adapter.hub.sessions.values()}
    offline = [{"device_id": device_id, "name": rec.get("name") or device_id, "last_seen": rec.get("last_seen")}
               for device_id, rec in adapter.hub.store.devices().items() if device_id not in online]
    return json.dumps({"success": True, "connected": list(online.values()), "offline": offline})


def hermlet_action(args: dict, **_kw) -> str:
    adapter, session, err = _target(args)
    if err:
        return _error(err)
    action = str(args.get("action") or "").strip()
    if not action:
        return _error("action is required")
    call_args = args.get("args") or {}
    if not isinstance(call_args, dict):
        return _error("args must be an object")
    try:
        result = _run(adapter, session.invoke(action, call_args, timeout=CALL_TIMEOUT_S - 2))
    except Exception as exc:
        return _error(str(exc))
    return json.dumps({"success": True, "device": session.name, "action": action, "result": result})


def hermlet_show(args: dict, **_kw) -> str:
    adapter, session, err = _target(args)
    if err:
        return _error(err)
    text, title = textfmt.plain(str(args.get("text") or "")), textfmt.plain(str(args.get("title") or ""))
    if not text and not title:
        return _error("text is required")
    try:
        seconds = max(1, int(args.get("seconds", 20)))
    except (TypeError, ValueError):
        seconds = 20
    try:
        _run(adapter, session.send_card(title, text, seconds))
    except Exception as exc:
        return _error(f"could not reach {session.name}: {exc}")
    return json.dumps({"success": True, "device": session.name, "shown_for_s": seconds})


def available() -> bool:
    """The tools work only in the process hosting the hub."""
    return runtime.active() is not None


def register_tools(ctx) -> None:
    for schema, handler, emoji in (
        (DEVICES_SCHEMA, hermlet_devices, "🐣"),
        (ACTION_SCHEMA, hermlet_action, "🎛️"),
        (SHOW_SCHEMA, hermlet_show, "🖼️"),
    ):
        ctx.register_tool(
            name=schema["name"], toolset=TOOLSET, schema=schema, handler=handler,
            check_fn=available, description=schema["description"], emoji=emoji)
