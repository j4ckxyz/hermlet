"""A stand-in for Hermes, for building and testing hermlet apps with no agent.

``hermlet devserver`` runs the real device hub (the same code the Hermes
plugin runs) with a scripted brain behind it. It echoes what you send and
reacts to a few words so every part of an app can be exercised:

    card        puts a card on the device
    file        sends a small text file back
    ask         asks a yes/no question, then reports the answer
    action X    calls the device action X and reports its result
    slow        takes a few seconds, with status updates, so "thinking" shows
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import secrets
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


def plugin(module: str):
    """Import a module of the Hermes plugin, installed or straight from a checkout."""
    try:
        return importlib.import_module(f"hermlet_plugin.{module}")
    except ImportError:
        pass
    plugin_dir = Path(__file__).resolve().parents[2] / "plugin"
    if "hermlet_plugin" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "hermlet_plugin", plugin_dir / "__init__.py", submodule_search_locations=[str(plugin_dir)])
        if spec is None or spec.loader is None:
            raise ImportError("the hermlet plugin is not available")
        package = importlib.util.module_from_spec(spec)
        sys.modules["hermlet_plugin"] = package
        spec.loader.exec_module(package)
    return importlib.import_module(f"hermlet_plugin.{module}")


def make_brain(require_pairing: bool = False, word_delay: float = 0.04, seed: Optional[str] = None):
    hub_mod, persona_mod, look_mod = plugin("hub"), plugin("persona"), plugin("look")

    class EchoBrain(hub_mod.HubDelegate):
        """Answers like a very small agent. ``approve()`` stands in for the owner."""

        def __init__(self) -> None:
            self.hub = None
            self.require_pairing = require_pairing
            self.word_delay = word_delay
            self.approved: set = set()
            self.codes: Dict[str, str] = {}
            self._persona = persona_mod.generate(seed)
            self._look = look_mod.clean({})
            self.memory_note = ""
            self.history: Dict[str, List[Dict[str, Any]]] = {}
            self.prompts: Dict[str, str] = {}
            self.events: List[Dict[str, Any]] = []

        def persona(self) -> dict:
            return self._persona

        def agent_name(self) -> str:
            return "Dev Hermes"

        async def is_paired(self, session) -> bool:
            return not self.require_pairing or session.device_id in self.approved

        async def on_ready(self, session) -> None:
            if not session.paired:
                code = self.codes.setdefault(session.device_id, secrets.token_hex(4).upper())
                await session.send_pairing(code, f"hermlet devserver: approve {code}")

        async def approve(self, code: str) -> bool:
            for device_id, known in self.codes.items():
                if known == code:
                    self.approved.add(device_id)
                    session = self.hub.get(device_id) if self.hub else None
                    if session:
                        await session.set_paired(True)
                    return True
            return False

        def _log(self, session, role: str, text: str) -> None:
            self.history.setdefault(session.device_id, []).append(
                {"id": len(self.history.get(session.device_id, [])) + 1, "role": role, "content": text,
                 "timestamp": time.time()})

        async def _reply(self, session, turn: str, text: str) -> None:
            shown = ""
            for word in text.split(" "):
                shown = (shown + " " + word).strip()
                await session.emit("reply.delta", turn=turn, text=shown)
                if self.word_delay:
                    await asyncio.sleep(self.word_delay)
            await session.emit("reply", turn=turn, text=text)
            self._log(session, "assistant", text)

        async def on_message(self, session, msg_id: str, text: str, attachments) -> None:
            turn = uuid.uuid4().hex[:8]
            self._log(session, "user", text)
            await session.emit("turn.start", turn=turn)
            low = text.lower()
            try:
                if "slow" in low:
                    for phrase in ("Thinking it over", "Looking around", "Nearly there"):
                        await session.emit("status", text=phrase)
                        await asyncio.sleep(1.0)
                    await session.emit("status", text="")
                if attachments:
                    listing = ", ".join(f"{a.name} ({a.mime or 'unknown'}, {len(a.data)} bytes, "
                                        f"sha256 {hashlib.sha256(a.data).hexdigest()[:12]})" for a in attachments)
                    await self._reply(session, turn, f"I received {len(attachments)} file(s): {listing}. Echo: {text}")
                elif low.startswith("action "):
                    name = text.split(None, 1)[1].strip()
                    try:
                        result = await session.invoke(name, {})
                        await self._reply(session, turn, f"{name} returned {result!r}")
                    except hub_mod.ActionError as exc:
                        await self._reply(session, turn, f"{name} failed: {exc}")
                elif "card" in low:
                    await session.send_card("A card", "This is what a card from the agent looks like.", ttl_s=20)
                    await self._reply(session, turn, "I put a card on your screen.")
                elif "file" in low:
                    await session.send_file("hello.txt", "text/plain", b"Hello from the dev server.\n",
                                            caption="A file for you", turn=turn)
                    await self._reply(session, turn, "I sent you a file.")
                elif low.startswith("ask"):
                    prompt_id = uuid.uuid4().hex[:8]
                    self.prompts[prompt_id] = turn
                    await session.emit("prompt", id=prompt_id, title="A question", text="Shall I go ahead?",
                                       choices=["yes", "no"], ttl_s=120)
                    await self._reply(session, turn, "I asked you a question.")
                else:
                    await self._reply(session, turn, f"Echo: {text}")
            finally:
                await session.emit("turn.end", turn=turn, outcome="success")

        async def on_utterance(self, session, msg_id: str, wav: bytes, seconds: float) -> None:
            heard = f"(a voice message, {seconds:.1f} seconds)"
            await session.emit("transcript", text=heard)
            await self.on_message(session, msg_id, heard, [])
            if session.has_speaker:
                await session.play_wav(wav)

        async def on_cancel(self, session) -> None:
            await session.send_notice("Stopped")

        async def on_new_session(self, session) -> None:
            self.history.pop(session.device_id, None)
            await session.send_notice("Started a new conversation")

        async def on_prompt_reply(self, session, prompt_id: str, answer: str) -> None:
            if self.prompts.pop(prompt_id, None) is None:
                await session.send_notice("That question has expired")
            else:
                await session.emit("reply", text=f"You answered {answer}.")

        async def on_event(self, session, name: str, data: Any, notify: bool) -> None:
            self.events.append({"device": session.device_id, "name": name, "data": data, "notify": notify})
            if notify:
                await self.on_message(session, "ev", f"[event] {name} {data!r}", [])

        def look(self) -> dict:
            return self._look

        async def on_look_set(self, session, changes, device) -> dict:
            self._look = look_mod.merged(self._look, changes, device)
            return {"look": self._look}

        async def on_persona_set(self, session, name, colour) -> dict:
            self._persona = persona_mod.generate(
                self._persona["seed"], name=persona_mod.clean_name(name) or self._persona["name"],
                species=self._persona["species"], colour=persona_mod.colour_hex(colour) or self._persona["colour"],
                chosen=True)
            self.memory_note = persona_mod.memory_note(self._persona)
            return {"persona": self._persona, "memory": "saved"}

        async def on_sessions(self, session, op: str, request: dict) -> dict:
            now = time.time()
            mine = self.history.get(session.device_id, [])
            listing = [
                {"id": "dev-current", "source": "hermlet", "title": f"{session.name} (this device)",
                 "started_at": now - 600, "last_active": now, "message_count": len(mine),
                 "preview": mine[0]["content"][:80] if mine else "", "current": True},
                {"id": "dev-discord", "source": "discord", "title": "Trip planning", "started_at": now - 86400,
                 "last_active": now - 80000, "message_count": 2, "preview": "Find me a train to Lisbon"},
                {"id": "dev-cli", "source": "cli", "title": "Fix the backup script", "started_at": now - 172800,
                 "last_active": now - 170000, "message_count": 2, "preview": "The nightly backup failed"},
            ]
            canned = {
                "dev-discord": [("user", "Find me a train to Lisbon"), ("assistant", "There is one at 9:40 from Porto.")],
                "dev-cli": [("user", "The nightly backup failed"), ("assistant", "The disk was full; I cleared the cache.")],
            }
            if op == "list":
                source, query = request.get("source"), str(request.get("query") or "").lower()
                rows = [r for r in listing if (not source or r["source"] == source)
                        and (not query or query in (r["title"] + r["preview"]).lower())]
                return {"sessions": rows[int(request.get("offset") or 0):][:int(request.get("limit") or 20)]}
            session_id = str(request.get("session_id") or "")
            row = next((r for r in listing if r["id"] == session_id), None)
            if row is None:
                return {"error": "no such conversation"}
            if op == "get":
                messages = mine if session_id == "dev-current" else [
                    {"id": i + 1, "role": role, "content": content, "timestamp": row["started_at"] + i}
                    for i, (role, content) in enumerate(canned[session_id])]
                return {"session": row, "messages": messages}
            return {"session": row, "resumed": True}

    return EchoBrain()


async def serve(host: str = "127.0.0.1", port: int = 0, *, require_pairing: bool = False, state_dir: Optional[str] = None,
                token: Optional[str] = None, word_delay: float = 0.04, seed: Optional[str] = None):
    """Start a hub with the echo brain. Returns ``(hub, brain)``; ``await hub.stop()`` when done."""
    import tempfile

    hub_mod, store_mod = plugin("hub"), plugin("store")
    brain = make_brain(require_pairing, word_delay, seed)
    store = store_mod.DeviceStore(state_dir or tempfile.mkdtemp(prefix="hermlet-dev-"))
    hub = hub_mod.DeviceHub(store, brain, host=host, port=port, access_token=token)
    brain.hub = hub
    await hub.start()
    return hub, brain
