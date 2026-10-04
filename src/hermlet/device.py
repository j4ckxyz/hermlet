"""``HubDevice``: a hermlet connected through the optional Hermes plugin.

The plugin adds what Hermes's own web API cannot do: the agent can call
actions on the device, reach it unprompted (cron results, messages from other
chats), and devices are approved with a pairing code instead of a password.
This class is the whole device side of that protocol (``docs/protocol.md``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from . import __version__, protocol
from .base import CONNECTING, PAIRING, READY, BaseDevice, FileRef, HermletError, read_ref
from .identity import Identity

log = logging.getLogger("hermlet")


class HubDevice(BaseDevice):
    mode = "plugin"

    def __init__(self, url: Optional[str] = None, name: Optional[str] = None, *,
                 home: Optional[Union[str, Path]] = None, token: Optional[str] = None, **kw: Any):
        super().__init__(name, home=home, **kw)
        self.url = protocol.normalize_url(url or self.cfg.get("url") or "")
        self.token = token if token is not None else (self.cfg.get("token") or None)
        self.identity = Identity.load(self.home)
        self._ws = None
        self._send_lock = asyncio.Lock()
        self._waiters: Dict[str, asyncio.Future] = {}
        self._files: Dict[int, Dict[str, Any]] = {}
        self._audio_stream: Optional[int] = None
        self._stream = 0

    @property
    def device_id(self) -> str:
        return self.identity.device_id

    def _actions_changed(self) -> None:
        if self._ws is not None and self.state in (PAIRING, READY):
            asyncio.ensure_future(self._send(protocol.message("actions", actions=self._manifest())))

    async def _close(self) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def _session(self) -> None:
        from websockets.asyncio.client import connect

        await self._set_state(CONNECTING)
        async with connect(self.url, subprotocols=[protocol.SUBPROTOCOL], max_size=protocol.MAX_FRAME_BYTES,
                           open_timeout=15, ping_interval=20, ping_timeout=40) as ws:
            await ws.send(protocol.message(
                "hello", proto=protocol.PROTO, device_id=self.device_id, name=self.name, kind=self.kind,
                sdk=__version__, token=self.token, caps=self.caps, actions=self._manifest(),
                sensors=self.sensors or None))
            challenge = self._expect(await asyncio.wait_for(ws.recv(), 15), "challenge")
            if challenge.get("enrolled"):
                await ws.send(protocol.message(
                    "auth", mac=protocol.mac_for(self.identity.key, self.device_id, str(challenge.get("nonce")))))
            elif self.identity.is_enrolled(self.url):
                raise HermletError(
                    "This Hermes no longer knows this device. If that is expected (its data was reset), "
                    "run 'hermlet setup --re-enroll' to introduce the device again.")
            else:
                await ws.send(protocol.message("auth", key=base64.b64encode(self.identity.key).decode()))
            welcome = self._expect(await asyncio.wait_for(ws.recv(), 15), "welcome")
            self.identity.mark_enrolled(self.url)
            self._ws = ws
            self.agent = str(welcome.get("agent") or "Hermes")
            self.limits = welcome.get("limits") or {}
            if self._remember_persona(welcome.get("persona")):
                await self._emit("persona", persona=self.persona)  # apps already open learn who this Hermes is
            await self._take_look(welcome.get("look") or {})
            if welcome.get("paired"):
                self.pairing = None
            await self._set_state(READY if welcome.get("paired") else PAIRING)
            try:
                async for frame in ws:
                    if isinstance(frame, (bytes, bytearray)):
                        await self._on_binary(bytes(frame))
                    else:
                        await self._on_text(frame)
            finally:
                self._ws = None
                self._fail_waiters("the connection to Hermes dropped")
                await self.player.abort()

    @staticmethod
    def _expect(raw: Any, type_: str) -> Dict[str, Any]:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            raise HermletError("Hermes sent something that is not a hermlet message") from None
        if msg.get("type") == "error":
            raise HermletError(f"Hermes refused the connection: {msg.get('message') or msg.get('code')}")
        if msg.get("type") != type_:
            raise HermletError(f"expected '{type_}' from Hermes, got '{msg.get('type')}'")
        return msg

    # -- incoming -------------------------------------------------------------------

    async def _on_text(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            return
        kind = msg["type"]
        if kind == "pairing":
            self.pairing = {"code": str(msg.get("code") or ""), "command": str(msg.get("command") or "")}
            await self._set_state(PAIRING)
        elif kind == "paired":
            self.pairing = None
            await self._set_state(READY)
        elif kind == "unpaired":
            await self._set_state(PAIRING)
        elif kind == "persona":
            self._remember_persona(msg.get("persona"))
        elif kind == "look":
            await self._take_look(msg.get("look") or {})
            return
        elif kind == "action":
            asyncio.ensure_future(self._run_action(msg))
            return
        elif kind in ("upload.ok", "upload.error", "sessions.result", "persona.result", "look.result"):
            waiter = self._waiters.pop(f"{kind.split('.')[0]}:{msg.get('id')}", None)
            if waiter and not waiter.done():
                waiter.set_result(msg)
            return
        elif kind == "file.start":
            self._files[int(msg.get("stream") or 0)] = {"meta": msg, "data": bytearray()}
            return
        elif kind == "file.end":
            await self._finish_file(msg)
            return
        elif kind == "audio.start":
            self._audio_stream = None
            if self.cfg.get("speak") and await self.player.open(int(msg.get("rate") or 16000),
                                                               int(msg.get("channels") or 1)):
                self._audio_stream = int(msg.get("stream") or 0)
        elif kind == "audio.end":
            if self._audio_stream == int(msg.get("stream") or 0):
                self._audio_stream = None
                asyncio.ensure_future(self.player.close())
        elif kind == "audio.abort":
            self._audio_stream = None
            await self.player.abort()
        elif kind == "error":
            log.warning("Hermes: %s", msg.get("message") or msg.get("code"))
            waiter = self._waiters.pop(f"text:{msg.get('id')}", None)
            if waiter and not waiter.done():
                waiter.set_exception(HermletError(str(msg.get("message") or msg.get("code"))))
        await self._dispatch(msg)

    async def _on_binary(self, frame: bytes) -> None:
        try:
            channel, stream, _seq, payload = protocol.parse_binary(frame)
        except ValueError:
            return
        if channel == protocol.CHANNEL_AUDIO and stream == self._audio_stream:
            await self.player.write(payload)
        elif channel == protocol.CHANNEL_FILE and stream in self._files:
            self._files[stream]["data"] += payload

    async def _finish_file(self, msg: Dict[str, Any]) -> None:
        entry = self._files.pop(int(msg.get("stream") or 0), None)
        if entry is None:
            return
        meta, data = entry["meta"], bytes(entry["data"])
        claimed = str(msg.get("sha256") or "")
        if claimed and claimed != hashlib.sha256(data).hexdigest():
            log.warning("a file from Hermes arrived damaged and was dropped: %s", meta.get("name"))
            return
        await self._save_file(str(meta.get("name") or "file"), data, str(meta.get("mime") or ""),
                              str(meta.get("caption") or ""), meta.get("turn"))

    async def _run_action(self, msg: Dict[str, Any]) -> None:
        call_id, name = msg.get("id"), str(msg.get("name") or "")
        try:
            result = await self.run_action(name, msg.get("args") if isinstance(msg.get("args"), dict) else {})
            await self._send(protocol.message("action.result", id=call_id, ok=True, result=result))
        except Exception as exc:
            log.info("action %s failed: %s", name, exc)
            await self._send(protocol.message("action.result", id=call_id, ok=False, error=str(exc) or exc.__class__.__name__))

    # -- outgoing -------------------------------------------------------------------

    async def _send(self, frame: Union[str, bytes]) -> None:
        ws = self._ws
        if ws is None:
            raise HermletError("not connected to Hermes")
        async with self._send_lock:
            await ws.send(frame)

    def _next_stream(self) -> int:
        self._stream = (self._stream + 1) & 0xFF
        return self._stream

    def _wait(self, key: str) -> "asyncio.Future[Dict[str, Any]]":
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters[key] = future
        return future

    def _fail_waiters(self, reason: str) -> None:
        for future in self._waiters.values():
            if not future.done():
                future.set_exception(HermletError(reason))
        self._waiters.clear()

    async def send(self, text: str = "", attachments: Sequence[FileRef] = ()) -> str:
        """Send a message, with any files or images alongside. Returns the message id."""
        self._require_ready()
        upload_ids = [await self.upload(ref) for ref in attachments]
        msg_id = "m" + uuid.uuid4().hex[:10]
        await self._send(protocol.message("text", id=msg_id, text=text, attachments=upload_ids or None))
        return msg_id

    async def upload(self, ref: FileRef) -> str:
        """Send one file ahead of the message that will mention it. Returns the upload id."""
        name, data = await read_ref(ref)
        limit = int(self.limits.get("upload_bytes") or 0)
        if limit and len(data) > limit:
            raise HermletError(f"{name} is too large ({len(data) / 1e6:.1f} MB; the limit is {limit / 1e6:.0f} MB)")
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        upload_id, stream = "u" + uuid.uuid4().hex[:10], self._next_stream()
        waiter = self._wait(f"upload:{upload_id}")
        await self._send(protocol.message("upload.start", id=upload_id, stream=stream, name=name, mime=mime, size=len(data)))
        for seq, start in enumerate(range(0, len(data), protocol.FILE_CHUNK_BYTES)):
            await self._send(protocol.binary(protocol.CHANNEL_FILE, stream, seq, data[start:start + protocol.FILE_CHUNK_BYTES]))
        await self._send(protocol.message("upload.end", id=upload_id, stream=stream, sha256=hashlib.sha256(data).hexdigest()))
        result = await asyncio.wait_for(waiter, 120)
        if result["type"] != "upload.ok":
            raise HermletError(f"Hermes did not accept {name}: {result.get('error')}")
        return upload_id

    def voice(self, rate: int = 16000) -> "Utterance":
        """Start a spoken message: ``async with device.voice() as v: await v.write(pcm)``."""
        return Utterance(self, rate)

    async def cancel(self) -> None:
        """Stop what Hermes is doing for this device."""
        await self.player.abort()
        await self._send(protocol.message("cancel"))

    async def new_session(self) -> None:
        """Start a fresh conversation."""
        await self._send(protocol.message("session.new"))

    async def answer(self, prompt_id: str, answer: str) -> None:
        """Answer a ``prompt``: ``yes``/``no``, or one of the choices it listed."""
        await self._send(protocol.message("prompt.reply", id=prompt_id, answer=answer))

    async def event(self, name: str, data: Any = None, notify: bool = False) -> None:
        """Report that something happened. With ``notify`` the agent is told, as a message."""
        await self._send(protocol.message("event", name=name, data=data, notify=notify or None))

    async def report(self, **sensors: Any) -> None:
        """Share readings (``temperature_c=21.5``) the agent can look up."""
        self.sensors.update(sensors)
        if self._ws is not None:
            await self._send(protocol.message("state", sensors=sensors))

    async def set_persona(self, name: Optional[str] = None, colour: Optional[str] = None) -> Dict[str, Any]:
        self._require_ready()
        req = "p" + uuid.uuid4().hex[:10]
        waiter = self._wait(f"persona:{req}")
        await self._send(protocol.message("persona.set", id=req, name=name, colour=colour))
        result = await asyncio.wait_for(waiter, 30)
        if result.get("error"):
            raise HermletError(str(result["error"]))
        self._remember_persona(result.get("persona"))
        return {"persona": self.persona, "memory": str(result.get("memory") or "")}

    async def set_look(self, changes: Dict[str, Any], this_device_only: bool = False) -> Dict[str, Any]:
        self._require_ready()
        req = "l" + uuid.uuid4().hex[:10]
        waiter = self._wait(f"look:{req}")
        await self._send(protocol.message("look.set", id=req, changes=changes, device=self.name if this_device_only else None))
        result = await asyncio.wait_for(waiter, 30)
        if result.get("error"):
            raise HermletError(str(result["error"]))
        await self._take_look(result.get("look") or {})
        return self.look

    async def teach_agent(self) -> bool:
        return True  # the plugin installs the skill itself when it starts

    async def _sessions(self, op: str, **fields: Any) -> Dict[str, Any]:
        req = "s" + uuid.uuid4().hex[:10]
        waiter = self._wait(f"sessions:{req}")
        await self._send(protocol.message(f"sessions.{op}", id=req, **fields))
        result = await asyncio.wait_for(waiter, 30)
        if result.get("error"):
            raise HermletError(str(result["error"]))
        return result

    async def sessions(self, limit: int = 20, offset: int = 0, source: Optional[str] = None,
                       query: Optional[str] = None) -> List[Dict[str, Any]]:
        """Past conversations with this agent from every surface: Discord, the web, other hermlets."""
        result = await self._sessions("list", limit=limit, offset=offset, source=source, query=query)
        return list(result.get("sessions") or [])

    async def session_messages(self, session_id: str, limit: int = 50, before: Optional[int] = None) -> Dict[str, Any]:
        """One conversation's messages, oldest first: ``{"session": {...}, "messages": [...]}``."""
        return await self._sessions("get", session_id=session_id, limit=limit, before=before)

    async def resume(self, session_id: str) -> Dict[str, Any]:
        """Carry on a past conversation on this device."""
        return await self._sessions("resume", session_id=session_id)


class Utterance:
    """One spoken message being streamed to Hermes, which transcribes it."""

    def __init__(self, device: "HubDevice", rate: int):
        self._device = device
        self.rate = rate
        self.id = "a" + uuid.uuid4().hex[:10]
        self._stream = device._next_stream()
        self._seq = 0
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
        if self._device.state != READY:
            raise HermletError("not connected to Hermes")
        await self._device._send(protocol.message("audio.start", id=self.id, stream=self._stream, rate=self.rate, format="pcm16"))
        self._open = True

    async def write(self, pcm: bytes) -> None:
        if self._open and pcm:
            await self._device._send(protocol.binary(protocol.CHANNEL_AUDIO, self._stream, self._seq, pcm))
            self._seq += 1

    async def end(self) -> None:
        if self._open:
            self._open = False
            await self._device._send(protocol.message("audio.end", id=self.id, stream=self._stream))

    async def discard(self) -> None:
        if self._open:
            self._open = False
            try:
                await self._device._send(protocol.message("audio.cancel", id=self.id, stream=self._stream))
            except HermletError:
                pass
