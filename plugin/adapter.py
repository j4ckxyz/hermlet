"""Hermes gateway platform adapter for hermlets.

Each connected device is a direct-message chat whose ``chat_id`` and
``user_id`` are the device id, so everything Hermes does for a messaging
platform applies unchanged: sessions and memory, ``/new``, ``/stop``, voice
notes transcribed by the gateway, DM pairing codes, approvals, cron delivery.

config.yaml::

    platforms:
      hermlet:
        enabled: true
        extra:
          host: 0.0.0.0          # or the machine's Tailscale address
          port: 8790
          auto_home: true        # first approved device becomes the hermlet home channel
          unauthorized_dm_behavior: pair
          history: all           # which past conversations devices may browse: all, own or off

Secrets (``~/.hermes/.env``): ``HERMLET_ACCESS_TOKEN`` (optional shared token).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import extra_or_secret, get_scoped_secret
from gateway.platforms.base import (
    BasePlatformAdapter,
    ExecApprovalPrompt,
    SendResult,
    cache_audio_from_bytes_async,
    cache_media_bytes_async,
)
from gateway.platforms.event import MessageEvent, MessageType

from . import look as look_mod, runtime, textfmt
from .hub import Attachment, DeviceHub, DeviceSession, HubDelegate
from .persona import MEMORY_MARK, PersonaStore, clean_name, colour_hex, colour_name, generate, memory_note
from .store import DeviceStore

logger = logging.getLogger(__name__)

PLATFORM_NAME = "hermlet"
PAIRING_TTL_S = 3600          # mirrors Hermes's pairing-code lifetime
POLL_S = 2.0
PAIRING_GRACE_S = 6.0
# Trigger text for Hermes's unauthorized-DM flow. It never reaches the agent while the
# device is unpaired; if approval races it, it is a cheap read-only command.
PAIRING_TRIGGER = "/status"
_PAIRING_CMD = re.compile(r"(hermes\s+(?:-p\s+\S+\s+)?pairing\s+approve\s+\S+\s+([A-Z2-9]{8}))\b")
AUTO_CONFIRM_NEW_S = 15.0     # a device's own "new conversation" button confirms itself
PROMPT_PREFIX = "prompt:"
SLASH_CONFIRM_TTL_S = 300
QUIET_SOURCES = ["cron", "subagent", "kanban", "tool"]


def _flag(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class _Prompt:
    """A question shown on a device, and how to hand its answer back to Hermes."""
    id: str
    kind: str  # "slash", "approval" or "clarify"
    session_key: str
    title: str
    text: str
    choices: List[str]
    confirm_id: str = ""
    created: float = field(default_factory=time.monotonic)


class HermletAdapter(BasePlatformAdapter, HubDelegate):
    MAX_MESSAGE_LENGTH = 8000
    supports_status_text = True

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform(PLATFORM_NAME))
        extra = config.extra or {}
        self._host = str(extra.get("host") or "0.0.0.0")
        port = extra.get("port")
        self._port = 8790 if port in (None, "") else int(port)
        self._path = str(extra.get("path") or "/hermlet")
        self._auto_home = _flag(extra.get("auto_home"), True)
        self._history = str(extra.get("history") or "all").lower()
        self._max_upload_mb = float(extra.get("max_upload_mb") or 25)
        self._access_token = extra_or_secret(extra, "access_token", "HERMLET_ACCESS_TOKEN") or None
        self._hub: Optional[DeviceHub] = None
        self._store: Optional[DeviceStore] = None
        self._personas: Optional[PersonaStore] = None
        self._persona: Dict[str, Any] = {}
        self._persona_mtime = 0.0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._turns: Dict[str, str] = {}
        self._watch_task: Optional[asyncio.Task] = None
        self._registry_key = "default"
        self._prompts: Dict[str, List[_Prompt]] = {}
        self._new_requested: Dict[str, float] = {}

    # -- lifecycle ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        from plugins.plugin_storage import plugin_data_dir

        data_dir = plugin_data_dir(PLATFORM_NAME)
        self._store = DeviceStore(data_dir)
        self._personas = PersonaStore(data_dir)
        self._persona = self._personas.load()
        self._persona_mtime = self._personas.mtime()
        self._look_path = data_dir / "look.json"
        self._look = self._read_look()
        self._look_mtime = self._mtime(self._look_path)
        await asyncio.to_thread(self._install_skill)
        self._hub = DeviceHub(self._store, self, host=self._host, port=self._port, path=self._path,
                              access_token=self._access_token, max_upload_mb=self._max_upload_mb)
        try:
            await self._hub.start()
        except OSError as exc:
            self._hub = None
            self._set_fatal_error("hermlet_listen_failed", f"Cannot listen on {self._host}:{self._port}: {exc}",
                                  retryable=True)
            return False
        self._loop = asyncio.get_running_loop()
        self._registry_key = getattr(self, "_owner_profile", None) or "default"
        runtime.register(self._registry_key, self)
        self._watch_task = asyncio.create_task(self._watch())
        self._mark_connected()
        logger.info("[%s] devices connect to ws://<this host>:%s%s", self.name, self._hub.bound_port, self._hub.path)
        return True

    async def disconnect(self) -> None:
        runtime.unregister(self._registry_key, self)
        if self._watch_task:
            self._watch_task.cancel()
            self._watch_task = None
        if self._hub:
            await self._hub.stop()
            self._hub = None
        self._mark_disconnected()

    @property
    def hub(self) -> Optional[DeviceHub]:
        return self._hub

    @property
    def loop(self) -> Optional[asyncio.AbstractEventLoop]:
        return self._loop

    def _session(self, chat_id: str) -> Optional[DeviceSession]:
        return self._hub.get(str(chat_id)) if self._hub else None

    # -- what the hub asks ------------------------------------------------------------

    def persona(self) -> dict:
        return self._persona

    # -- the look: one file every device reads, and the agent may edit ---------------------

    def look(self) -> dict:
        return self._look

    @staticmethod
    def _mtime(path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def _read_look(self) -> dict:
        try:
            return look_mod.parse(self._look_path.read_text(encoding="utf-8"))
        except OSError:
            return look_mod.clean({})

    def _write_look(self, look: dict) -> None:
        tmp = self._look_path.with_suffix(".tmp")
        tmp.write_text(look_mod.dumps(look), encoding="utf-8")
        os.replace(tmp, self._look_path)

    async def on_look_set(self, session: DeviceSession, changes: dict, device: Optional[str]) -> dict:
        self._look = look_mod.merged(self._read_look(), changes, device)
        await asyncio.to_thread(self._write_look, self._look)
        self._look_mtime = self._mtime(self._look_path)
        return {"look": self._look}

    def _install_skill(self) -> None:
        """The skill that tells the agent where the look file is, so "make the accent red" works."""
        try:
            from hermes_constants import get_hermes_home

            if not self._look_path.exists():
                self._write_look(self._look)
            target = get_hermes_home() / "skills" / look_mod.SKILL_NAME / "SKILL.md"
            text = look_mod.skill(str(self._look_path))
            if not target.exists() or target.read_text(encoding="utf-8") != text:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
        except Exception as exc:
            logger.info("[%s] could not install the hermlet skill: %s", self.name, exc)

    def agent_name(self) -> str:
        return "Hermes"

    async def on_persona_set(self, session: DeviceSession, name: Optional[str], colour: Optional[str]) -> dict:
        """The owner named or coloured the critter on a device: keep it, and tell the agent's notes."""
        current = self._persona
        persona = generate(current["seed"], name=clean_name(name) or current["name"], species=current["species"],
                           colour=colour_hex(colour) or current.get("colour"), chosen=True)
        await asyncio.to_thread(self._personas.save, persona)
        self._persona, self._persona_mtime = persona, self._personas.mtime()
        logger.info("[%s] the critter is now %s (%s)", self.name, persona["name"], persona["colour"])
        return {"persona": persona, "memory": await asyncio.to_thread(self._note_in_memory, persona)}

    @staticmethod
    def _note_in_memory(persona: dict) -> str:
        """One line in the agent's own notes (MEMORY.md), through Hermes's own memory store."""
        try:
            from hermes_cli.config import load_config
            from tools.memory_tool_store import MemoryStore

            settings = (load_config() or {}).get("memory") or {}
            if settings.get("memory_enabled") is False:
                return "off"
            store = MemoryStore(int(settings.get("memory_char_limit") or 2200), int(settings.get("user_char_limit") or 1375))
            store.load_from_disk()
            note = memory_note(persona)
            known = any(str(entry).lstrip().startswith(MEMORY_MARK) for entry in store.memory_entries)
            result = store.replace("memory", MEMORY_MARK, note) if known else store.add("memory", note)
            if result.get("success"):
                return "saved"
            logger.info("could not note the critter in memory: %s", str(result.get("error"))[:200])
            return "full" if "limit" in str(result.get("error") or "").lower() else "failed"
        except Exception as exc:
            logger.info("could not note the critter in memory: %s", exc)
            return "failed"

    def _critter_context(self) -> str:
        persona = self._persona or {}
        if not persona.get("name"):
            return ""
        shade = colour_name(persona.get("colour"))
        return (f" On its screen you appear as a pixel critter named {persona['name']}"
                f" ({(shade + ' ') if shade else ''}{persona.get('species')}). Requests made here to change how the "
                f"app looks (theme, colours, accent, font, text size) mean the Hermlet app: edit {self._look_path} "
                f"as the '{look_mod.SKILL_NAME}' skill describes, never Hermes's own skin or config.")

    def _authorized(self, device_id: str) -> Optional[bool]:
        return self._is_sender_authorized(device_id, "dm", device_id)

    async def is_paired(self, session: DeviceSession) -> bool:
        # None means no runner check is installed (unit harness): nothing to gate on.
        return self._authorized(session.device_id) is not False

    async def on_ready(self, session: DeviceSession) -> None:
        if session.paired:
            self._store.clear_pairing(session.device_id)
            await self._claim_home(session)
            await self._show_prompt(session)  # a question asked while the device was away
            return
        cached = self._store.pairing_for(session.device_id)
        if cached:
            await session.send_pairing(*cached)
            return
        # Hermes answers an unauthorized DM with a pairing code; send() recognises it and
        # forwards it to the device as a pairing frame.
        await self.handle_message(MessageEvent(
            text=PAIRING_TRIGGER, message_type=MessageType.TEXT,
            source=self._source(session), message_id=self._message_id(session, "pair")))
        session.spawn(self._pairing_grace(session))

    async def _pairing_grace(self, session: DeviceSession) -> None:
        await asyncio.sleep(PAIRING_GRACE_S)
        if session.closed or session.paired or self._store.pairing_for(session.device_id):
            return
        await session.send_notice(
            f"No pairing code yet. On the Hermes host run 'hermes pairing list', or allow "
            f"{session.device_id} in HERMLET_ALLOWED_USERS.", ttl_s=60)

    async def _watch(self) -> None:
        """Mirror pairing approvals and revocations, and persona edits, onto connected devices."""
        while True:
            await asyncio.sleep(POLL_S)
            if not self._hub:
                continue
            mtime = self._personas.mtime()
            if mtime != self._persona_mtime:  # `hermes hermlet persona` changed the critter
                self._persona_mtime = mtime
                self._persona = self._personas.load()
                await self._hub.broadcast_persona()
            mtime = self._mtime(self._look_path)
            if mtime != self._look_mtime:  # the agent (or a person) edited the look
                self._look_mtime = mtime
                self._look = self._read_look()
                await self._hub.broadcast_look()
            for session in list(self._hub.sessions.values()):
                verdict = self._authorized(session.device_id)
                if verdict is None:
                    continue
                try:
                    if verdict and not session.paired:
                        self._store.clear_pairing(session.device_id)
                        await session.set_paired(True)
                        logger.info("[%s] device %s approved", self.name, session.device_id)
                        await self._claim_home(session)
                    elif not verdict and session.paired:
                        await session.set_paired(False)
                        await self.on_ready(session)
                except Exception as exc:
                    logger.debug("[%s] pairing sync failed for %s: %s", self.name, session.device_id, exc)

    async def _claim_home(self, session: DeviceSession) -> None:
        """Make the first approved device the platform's home channel, as /sethome would.

        Without one, Hermes opens every new session with a "type /sethome" notice.
        """
        if not self._auto_home or self.config.home_channel is not None:
            return
        if str(get_scoped_secret("HERMLET_HOME_CHANNEL", "") or "").strip():
            return
        from gateway import config as gateway_config

        home = gateway_config.HomeChannel(platform=self.platform, chat_id=session.device_id,
                                          name=session.name, user_id=session.device_id)
        self.config.home_channel = home
        try:
            await asyncio.to_thread(gateway_config.persist_home_channel, home)
        except Exception as exc:  # the running gateway still has it; only a restart forgets
            logger.warning("[%s] could not save home channel %s: %s", self.name, session.device_id, exc)

    # -- inbound --------------------------------------------------------------------

    def _source(self, session: DeviceSession):
        return self.build_source(chat_id=session.device_id, chat_name=session.name, chat_type="dm",
                                 user_id=session.device_id, user_name=session.name)

    @staticmethod
    def _message_id(session: DeviceSession, msg_id: str) -> str:
        return f"{session.session_id}:{msg_id or uuid.uuid4().hex[:8]}"

    def _event(self, session: DeviceSession, msg_id: str, text: str, **kw) -> MessageEvent:
        return MessageEvent(
            text=text, source=self._source(session), message_id=self._message_id(session, msg_id),
            user_id=session.device_id, user_name=session.name,
            channel_prompt=session.prompt_context() + self._critter_context(), **kw)

    async def on_message(self, session: DeviceSession, msg_id: str, text: str, attachments: List[Attachment]) -> None:
        paths, mimes, kinds = [], [], set()
        for item in attachments:
            cached = await cache_media_bytes_async(item.data, filename=item.name, mime_type=item.mime)
            if cached is None:
                await session.send_notice(f"Hermes could not read {item.name}")
                continue
            paths.append(cached.path)
            mimes.append(cached.media_type)
            kinds.add(cached.kind)
        kind = MessageType.TEXT
        if paths:
            kind = MessageType.PHOTO if kinds == {"image"} else MessageType.DOCUMENT
        await self.handle_message(self._event(session, msg_id, text, message_type=kind,
                                              media_urls=paths, media_types=mimes))

    async def on_utterance(self, session: DeviceSession, msg_id: str, wav: bytes, seconds: float) -> None:
        path = await cache_audio_from_bytes_async(wav, ".wav")
        await self.handle_message(self._event(
            session, msg_id, "", message_type=MessageType.VOICE, media_urls=[path], media_types=["audio/wav"]))

    async def on_cancel(self, session: DeviceSession) -> None:
        await self.handle_message(self._event(session, "cancel", "/stop", message_type=MessageType.TEXT))

    async def on_new_session(self, session: DeviceSession) -> None:
        self._new_requested[session.device_id] = time.monotonic()
        await self.handle_message(self._event(session, "new", "/new", message_type=MessageType.TEXT))

    async def on_event(self, session: DeviceSession, name: str, data: Any, notify: bool) -> None:
        logger.info("[%s] event from %s: %s", self.name, session.device_id, name)
        if not notify:
            return
        payload = json.dumps(data, separators=(",", ":"))[:500] if data is not None else ""
        await self.handle_message(self._event(
            session, f"ev-{uuid.uuid4().hex[:6]}", f"[{session.name} event] {name} {payload}".strip(),
            message_type=MessageType.TEXT, allow_gateway_control=False))

    async def on_disconnect(self, session: DeviceSession) -> None:
        if self._hub and session.device_id not in self._hub.sessions:
            self._turns.pop(session.device_id, None)

    # -- turn lifecycle -------------------------------------------------------------

    async def on_processing_start(self, event: MessageEvent) -> None:
        session = self._session(event.source.chat_id)
        if session is None or not session.paired:
            return
        turn = event.message_id or uuid.uuid4().hex[:12]
        self._turns[session.device_id] = turn
        await session.emit("turn.start", turn=turn)

    async def on_processing_complete(self, event: MessageEvent, outcome) -> None:
        session = self._session(event.source.chat_id)
        if session is None or not session.paired:
            return
        turn = self._turns.pop(session.device_id, None) or event.message_id or ""
        await session.emit("turn.end", turn=turn, outcome=getattr(outcome, "value", str(outcome)))

    def set_status_text(self, chat_id: str, text: Optional[str]) -> None:
        super().set_status_text(chat_id, text)
        session = self._session(chat_id)
        if session is None or self._loop is None:
            return
        coro = session.emit("status", text=text or "")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            session.spawn(coro)
        else:
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    # -- outbound text ----------------------------------------------------------------

    @staticmethod
    def _shape(session: DeviceSession, content: str) -> str:
        return content if session.wants_markdown else textfmt.plain(content)

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        session = self._session(chat_id)
        if session is None:
            return SendResult(success=False, error=f"hermlet {chat_id} is not connected")
        if not session.paired:
            match = _PAIRING_CMD.search(content or "")
            if match:
                self._store.remember_pairing(session.device_id, match.group(2), match.group(1), PAIRING_TTL_S)
                await session.send_pairing(match.group(2), match.group(1))
            else:
                await session.send_notice(textfmt.plain(content), ttl_s=30)
            return SendResult(success=True, message_id=uuid.uuid4().hex[:12])
        heard = self._transcript_echo(content or "")
        if heard is not None:
            await session.emit("transcript", text=heard)
            return SendResult(success=True, message_id=uuid.uuid4().hex[:12])
        text = self._shape(session, content)
        if text:
            interim = bool((metadata or {}).get("_interim_send"))
            await session.emit("reply", text=text, turn=self._turns.get(session.device_id), interim=interim or None)
        return SendResult(success=True, message_id=uuid.uuid4().hex[:12])

    @staticmethod
    def _transcript_echo(content: str) -> Optional[str]:
        """The transcript, if ``content`` is the gateway's "here is what I heard" line."""
        try:
            from agent.i18n import t

            line = t("gateway.voice.transcript_echo_short", text="\x00")
            head, slot, tail = line.partition("\x00")
            if slot and content.strip().startswith(head.strip()) and content.strip().endswith(tail.strip()):
                inner = content.strip()[len(head.strip()):]
                return (inner[:len(inner) - len(tail.strip())] if tail.strip() else inner).strip()
        except Exception:
            pass
        return None

    def supports_draft_streaming(self, chat_type=None, metadata=None, chat_id=None) -> bool:
        return True

    async def send_draft(self, chat_id: str, draft_id: int, content: str,
                         metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        session = self._session(chat_id)
        if session is None or not session.paired:
            return SendResult(success=False, error="hermlet not connected")
        await session.emit("reply.delta", text=self._shape(session, content), turn=self._turns.get(session.device_id))
        return SendResult(success=True)

    async def edit_message(self, chat_id: str, message_id: str, content: str, *, finalize: bool = False) -> SendResult:
        session = self._session(chat_id)
        if session is None or not session.paired:
            return SendResult(success=False, error="hermlet not connected")
        text = self._shape(session, content)
        if str(message_id).startswith(PROMPT_PREFIX):
            # Hermes edits a question when it times out: withdraw it and say why.
            await self._withdraw_prompt(session, str(message_id)[len(PROMPT_PREFIX):])
            if text:
                await session.send_notice(text, ttl_s=15)
            return SendResult(success=True, message_id=message_id)
        turn = self._turns.get(session.device_id)
        await session.emit("reply" if finalize else "reply.delta", text=text, turn=turn)
        return SendResult(success=True, message_id=message_id)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        session = self._session(chat_id)
        return {"name": session.name if session else chat_id, "type": "dm", "chat_id": chat_id}

    # -- outbound files, pictures and speech ---------------------------------------------

    async def _deliver(self, chat_id: str, path: str, caption: Optional[str], mime: str = "") -> SendResult:
        session = self._session(chat_id)
        if session is None or not session.paired:
            return SendResult(success=False, error="hermlet not connected")
        try:
            data = await asyncio.to_thread(lambda: open(path, "rb").read())
        except OSError as exc:
            return SendResult(success=False, error=str(exc))
        import mimetypes

        await session.send_file(os.path.basename(path), mime or mimetypes.guess_type(path)[0] or "application/octet-stream",
                                data, caption=caption, turn=self._turns.get(session.device_id))
        return SendResult(success=True, message_id=uuid.uuid4().hex[:12])

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None,
                              reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                              **kwargs) -> SendResult:
        return await self._deliver(chat_id, image_path, caption)

    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        from gateway.platforms.base import cache_image_from_url

        try:
            path = await cache_image_from_url(image_url)
        except Exception as exc:
            logger.info("[%s] image download failed (%s); sending the link", self.name, exc)
            return await self.send(chat_id, f"{caption or ''}\n{image_url}".strip(), metadata=metadata)
        return await self._deliver(chat_id, path, caption)

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None,
                            file_name: Optional[str] = None, reply_to: Optional[str] = None,
                            metadata: Optional[Dict[str, Any]] = None, **kwargs) -> SendResult:
        return await self._deliver(chat_id, file_path, caption)

    async def send_voice(self, chat_id: str, audio_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                         **kwargs) -> SendResult:
        session = self._session(chat_id)
        if session is None or not session.paired:
            return SendResult(success=False, error="hermlet not connected")
        if session.has_speaker and audio_path.lower().endswith(".wav"):
            try:
                await session.play_wav(await asyncio.to_thread(lambda: open(audio_path, "rb").read()),
                                       turn=self._turns.get(session.device_id))
                return SendResult(success=True, message_id=uuid.uuid4().hex[:12])
            except (OSError, ValueError, EOFError) as exc:
                logger.info("[%s] could not stream %s (%s); sending it as a file", self.name, audio_path, exc)
        return await self._deliver(chat_id, audio_path, caption)

    async def play_tts(self, chat_id: str, audio_path: str, **kwargs) -> SendResult:
        return await self.send_voice(chat_id, audio_path)

    # -- questions: confirmations, approvals, clarifications --------------------------------

    async def send_slash_confirm(self, chat_id: str, title: str, message: str, session_key: str,
                                 confirm_id: str, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        session = self._session(chat_id)
        if session is None or not session.paired:
            return SendResult(success=False, error="hermlet not connected")
        from tools import slash_confirm

        pending = slash_confirm.get_pending(session_key) or {}
        requested = self._new_requested.pop(session.device_id, None)
        if (pending.get("command") == "new" and pending.get("confirm_id") == confirm_id
                and requested is not None and time.monotonic() - requested <= AUTO_CONFIRM_NEW_S):
            # The device asked for this /new itself, with its "new conversation" button.
            session.spawn(self._answer_slash(session, session_key, confirm_id, "once"))
            return SendResult(success=True, message_id=uuid.uuid4().hex[:12])
        first = textfmt.plain(message).split("\n\n")[0]
        return await self._ask(session, _Prompt(
            id=uuid.uuid4().hex[:8], kind="slash", session_key=session_key, title=textfmt.plain(title) or "Confirm",
            text=first, choices=["yes", "no"], confirm_id=confirm_id))

    async def _send_exec_approval_prompt(self, prompt: ExecApprovalPrompt) -> SendResult:
        session = self._session(prompt.chat_id)
        if session is None or not session.paired:
            return SendResult(success=False, error="hermlet not connected")
        text = prompt.command.strip()
        if prompt.description:
            text = f"{text}\n{prompt.description}"
        choices = [choice for _label, choice, _style in (prompt.actions or [])] or ["once", "deny"]
        return await self._ask(session, _Prompt(
            id=uuid.uuid4().hex[:8], kind="approval", session_key=prompt.session_key,
            title="Allow this command?", text=text, choices=choices))

    async def send_clarify(self, chat_id: str, question: str, choices: Optional[list], clarify_id: str,
                           session_key: str, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        session = self._session(chat_id)
        if session is None or not session.paired or not choices:
            return await super().send_clarify(chat_id, question, choices, clarify_id, session_key, metadata)
        return await self._ask(session, _Prompt(
            id=uuid.uuid4().hex[:8], kind="clarify", session_key=session_key, title="Hermes asks",
            text=textfmt.plain(question), choices=[str(c) for c in choices], confirm_id=clarify_id))

    async def _ask(self, session: DeviceSession, prompt: _Prompt) -> SendResult:
        queue = self._prompts.setdefault(session.device_id, [])
        if prompt.kind == "slash":
            # Hermes keeps one confirmation per session: a new one supersedes the last.
            queue[:] = [p for p in queue if not (p.kind == "slash" and p.session_key == prompt.session_key)]
        queue.append(prompt)
        await self._show_prompt(session)
        return SendResult(success=True, message_id=PROMPT_PREFIX + prompt.id)

    async def _show_prompt(self, session: DeviceSession) -> None:
        """Put the oldest open question on the device (it shows one at a time)."""
        queue = self._prompts.get(session.device_id) or []
        queue[:] = [p for p in queue if p.kind != "slash" or time.monotonic() - p.created <= SLASH_CONFIRM_TTL_S]
        if queue:
            head = queue[0]
            await session.emit("prompt", id=head.id, title=head.title, text=head.text, choices=head.choices, kind=head.kind)

    def _take_prompt(self, device_id: str, prompt_id: str) -> Optional[_Prompt]:
        queue = self._prompts.get(device_id) or []
        for i, prompt in enumerate(queue):
            if prompt.id == prompt_id:
                return queue.pop(i)
        return None

    async def _withdraw_prompt(self, session: DeviceSession, prompt_id: str) -> None:
        queue = self._prompts.get(session.device_id) or []
        was_shown = bool(queue) and queue[0].id == prompt_id
        if self._take_prompt(session.device_id, prompt_id) and was_shown:
            await session.emit("prompt.close", id=prompt_id)
            await self._show_prompt(session)

    async def on_prompt_reply(self, session: DeviceSession, prompt_id: str, answer: str) -> None:
        prompt = self._take_prompt(session.device_id, prompt_id)
        answer = (answer or "").strip()
        yes = answer.lower() in ("yes", "y", "once", "ok")
        if prompt is None:
            await session.send_notice("That question has expired")
        elif prompt.kind == "slash":
            await self._answer_slash(session, prompt.session_key, prompt.confirm_id, "once" if yes else "cancel")
        elif prompt.kind == "clarify":
            from tools.clarify_gateway import resolve_gateway_clarify

            if not resolve_gateway_clarify(prompt.confirm_id, answer):
                await session.send_notice("That question has expired")
        else:
            from tools.approval import resolve_gateway_approval

            choice = answer if answer in ("once", "session", "always", "deny") else ("once" if yes else "deny")
            if not resolve_gateway_approval(prompt.session_key, choice):
                await session.send_notice("That approval has expired")
        await self._show_prompt(session)

    async def _answer_slash(self, session: DeviceSession, session_key: str, confirm_id: str, choice: str) -> None:
        from tools import slash_confirm

        try:
            result = await slash_confirm.resolve(session_key, confirm_id, choice)
        except Exception as exc:
            logger.warning("[%s] confirmation failed for %s: %s", self.name, session.device_id, exc)
            result = None
        if result:
            await self.send(session.device_id, result)
        else:
            await session.send_notice("That question has expired")

    # -- history from every surface -----------------------------------------------------

    def _db(self):
        from hermes_constants import get_hermes_home
        from hermes_state_registry import acquire

        return acquire(get_hermes_home() / "state.db")

    def _owns(self, session: DeviceSession, row: dict) -> bool:
        return row.get("source") == PLATFORM_NAME and str(row.get("chat_id") or row.get("user_id") or "") == session.device_id

    def _row(self, session: DeviceSession, row: dict, current_id: str = "") -> dict:
        preview = str(row.get("preview") or "").strip()[:160]
        return {"id": row.get("id"), "source": row.get("source") or "", "title": row.get("title") or preview[:60] or "Untitled",
                "preview": preview, "started_at": row.get("started_at") or 0,
                "last_active": row.get("last_active") or row.get("started_at") or 0,
                "message_count": row.get("message_count") or 0, "current": bool(current_id) and row.get("id") == current_id}

    async def on_sessions(self, session: DeviceSession, op: str, request: dict) -> dict:
        if self._history == "off":
            return {"error": "browsing past conversations is turned off on this Hermes"}
        db = await asyncio.to_thread(self._db)
        own_only = self._history == "own"
        if op == "list":
            limit = max(1, min(int(request.get("limit") or 20), 100))
            source = request.get("source") or (PLATFORM_NAME if own_only else None)
            rows = await asyncio.to_thread(
                lambda: db.list_sessions_rich(
                    source=source, exclude_sources=None if source else QUIET_SOURCES, limit=limit,
                    offset=int(request.get("offset") or 0), order_by_last_active=True,
                    search_query=request.get("query") or None))
            if own_only:
                rows = [r for r in rows if self._owns(session, r)]
            current = max((r for r in rows if self._owns(session, r)), key=lambda r: r.get("last_active") or 0, default=None)
            return {"sessions": [self._row(session, r, current["id"] if current else "") for r in rows]}
        session_id = str(request.get("session_id") or "")
        row = await asyncio.to_thread(db.get_session, session_id)
        if not row or (own_only and not self._owns(session, row)):
            return {"error": "no such conversation"}
        if op == "get":
            limit = max(1, min(int(request.get("limit") or 50), 500))
            raw = await asyncio.to_thread(lambda: db.get_messages(session_id, limit=limit, latest=True))
            messages = [{"id": m.get("id"), "role": m["role"], "content": m["content"], "timestamp": m.get("timestamp") or 0}
                        for m in raw if m.get("role") in ("user", "assistant")
                        and isinstance(m.get("content"), str) and m["content"].strip()]
            return {"session": self._row(session, row), "messages": messages}
        # resume: one of this device's own conversations is reopened; another surface's is
        # continued in a new conversation that starts with its history, so the original stays put.
        target = session_id
        if not self._owns(session, row):
            target = f"hermlet_{int(time.time())}_{uuid.uuid4().hex[:6]}"
            history = await asyncio.to_thread(lambda: db.get_messages(session_id))

            def branch() -> None:
                db.create_session(target, PLATFORM_NAME, model=row.get("model"), user_id=session.device_id,
                                  chat_id=session.device_id, chat_type="dm", parent_session_id=session_id,
                                  model_config={"_branched_from": session_id})
                db.replace_messages(target, history)

            await asyncio.to_thread(branch)
        await self.handle_message(self._event(session, f"resume-{uuid.uuid4().hex[:6]}", f"/resume {target}",
                                              message_type=MessageType.TEXT))
        return {"session": {**self._row(session, row), "id": target, "source": PLATFORM_NAME,
                            "continued_from": session_id, "current": True}}
