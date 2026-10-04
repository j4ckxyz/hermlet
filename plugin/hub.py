"""Device hub: the WebSocket endpoint hermlets connect to.

Owns transport, authentication, enrollment, uploads, voice capture assembly,
paced audio playback, files and device actions. It knows nothing about Hermes:
a ``HubDelegate`` decides what a device message means. The Hermes platform
adapter is one delegate; the development server (``hermlet devserver``) is
another, so both run this exact code.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import secrets
import time
import uuid
import wave
from dataclasses import dataclass, field
from typing import Any

from . import protocol
from .store import DeviceStore

log = logging.getLogger("hermlet.hub")

HANDSHAKE_TIMEOUT_S = 10.0
AUDIO_FRAME_MS = 40          # outbound audio frame duration
PLAYBACK_LEAD_S = 0.5        # how far ahead of real time playback audio is sent
MIN_UTTERANCE_S = 0.25
MAX_PENDING_UPLOADS = 8
ACTION_TIMEOUT_S = 20.0


@dataclass
class Attachment:
    """A file a device sent along with a message."""
    name: str
    mime: str
    data: bytes


class HubDelegate:
    """Decides what device traffic means. Every method has a harmless default."""

    def persona(self) -> dict:
        return {}

    def agent_name(self) -> str:
        return "Hermes"

    def look(self) -> dict:
        """The shared look file (see look.py), as stored."""
        return {}

    async def on_look_set(self, session: "DeviceSession", changes: dict, device: str | None) -> dict:
        """A device changed the look. Return ``{"look": <the whole file>}``."""
        return {"error": "the look cannot be changed here"}

    async def is_paired(self, session: "DeviceSession") -> bool:
        return True

    async def on_ready(self, session: "DeviceSession") -> None:
        pass

    async def on_message(self, session: "DeviceSession", msg_id: str, text: str,
                         attachments: list[Attachment]) -> None:
        pass

    async def on_utterance(self, session: "DeviceSession", msg_id: str, wav: bytes, seconds: float) -> None:
        pass

    async def on_cancel(self, session: "DeviceSession") -> None:
        pass

    async def on_new_session(self, session: "DeviceSession") -> None:
        pass

    async def on_prompt_reply(self, session: "DeviceSession", prompt_id: str, answer: str) -> None:
        pass

    async def on_event(self, session: "DeviceSession", name: str, data: Any, notify: bool) -> None:
        pass

    async def on_persona_set(self, session: "DeviceSession", name: str | None, colour: str | None) -> dict:
        """The owner named or coloured the critter. Return ``{"persona": ..., "memory": ...}``."""
        return {"error": "the critter cannot be changed here"}

    async def on_sessions(self, session: "DeviceSession", op: str, request: dict) -> dict:
        """``op`` is ``list``, ``get`` or ``resume``; the returned dict is sent back as-is."""
        return {"error": "conversation history is not available here"}

    async def on_disconnect(self, session: "DeviceSession") -> None:
        pass


class Rejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ActionError(RuntimeError):
    pass


@dataclass
class _Incoming:
    """Bytes arriving on a binary stream: an utterance or an upload."""
    id: str
    stream: int
    kind: str  # "audio" or "file"
    started: float
    name: str = ""
    mime: str = ""
    size: int = 0
    rate: int = 16000
    data: bytearray = field(default_factory=bytearray)
    overflow: bool = False


class AudioOut:
    """One outbound audio stream, paced so a device needs only a small buffer.

    Frames go out at most ``PLAYBACK_LEAD_S`` ahead of real time. ``finish()``
    returns immediately; the pump sends the tail and ``audio.end`` afterwards.
    """

    def __init__(self, session: "DeviceSession", stream: int, rate: int, channels: int = 1):
        self._session = session
        self.stream = stream
        self.rate = rate
        self.channels = channels
        self._frame_bytes = max(2, rate * 2 * channels * AUDIO_FRAME_MS // 1000)
        self._buf = bytearray()
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self.closed = False
        self.aborted = False
        self.done = asyncio.Event()

    async def start(self, *, turn: str | None = None) -> None:
        await self._session.send_json(protocol.message(
            "audio.start", stream=self.stream, rate=self.rate, channels=self.channels, format="pcm16", turn=turn))
        self._task = asyncio.create_task(self._pump())

    def write(self, pcm: bytes) -> None:
        if self.closed:
            return
        self._buf += pcm
        fb = self._frame_bytes
        while len(self._buf) >= fb:
            self._queue.put_nowait(bytes(self._buf[:fb]))
            del self._buf[:fb]

    def finish(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._buf:
            self._queue.put_nowait(bytes(self._buf))
            self._buf.clear()
        self._queue.put_nowait(None)

    async def abort(self) -> None:
        if self.aborted:
            return
        self.closed = self.aborted = True
        if self._task:
            self._task.cancel()
        try:
            await self._session.send_json(protocol.message("audio.abort", stream=self.stream))
        except Exception:
            pass
        self.done.set()

    async def _pump(self) -> None:
        loop = asyncio.get_running_loop()
        seq, sent_s, base = 0, 0.0, loop.time()
        try:
            while True:
                frame = await self._queue.get()
                if frame is None:
                    break
                ahead = sent_s - (loop.time() - base)
                if ahead < 0:
                    base += ahead  # producer stalled: rebase so we never burst past the lead
                elif ahead > PLAYBACK_LEAD_S:
                    await asyncio.sleep(ahead - PLAYBACK_LEAD_S)
                await self._session.send_binary(protocol.binary(protocol.CHANNEL_AUDIO, self.stream, seq, frame))
                seq += 1
                sent_s += len(frame) / (2 * self.channels) / self.rate
            await self._session.send_json(protocol.message("audio.end", stream=self.stream))
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # connection dropped mid-stream
            log.debug("audio pump stopped: %s", exc)
        finally:
            self.done.set()


class DeviceSession:
    """One authenticated device connection."""

    def __init__(self, hub: "DeviceHub", ws, hello: dict):
        self.hub = hub
        self._ws = ws
        self.session_id = uuid.uuid4().hex[:12]
        self.device_id: str = hello["device_id"]
        self.name: str = str(hello.get("name") or self.device_id)[:64]
        self.kind: str = str(hello.get("kind") or "linux")[:64]
        self.sdk: str = str(hello.get("sdk") or "")[:32]
        caps = hello.get("caps")
        self.caps: dict = caps if isinstance(caps, dict) else {}
        self.actions: list[dict] = _clean_actions(hello.get("actions"))
        sensors = hello.get("sensors")
        self.sensors: dict = dict(sensors) if isinstance(sensors, dict) else {}
        self.sensors_at: float = time.time() if self.sensors else 0.0
        self.paired = False
        self.connected_at = time.time()
        self.closed = False
        self._send_lock = asyncio.Lock()
        self._incoming: dict[int, _Incoming] = {}
        self._uploads: dict[str, Attachment] = {}
        self._pending_actions: dict[str, asyncio.Future] = {}
        self._tasks: set[asyncio.Task] = set()
        self._next_stream = 0
        self._audio_out: AudioOut | None = None

    # -- capabilities -----------------------------------------------------------

    @property
    def has_speaker(self) -> bool:
        return isinstance(self.caps.get("speaker"), dict)

    @property
    def has_mic(self) -> bool:
        return isinstance(self.caps.get("mic"), dict)

    @property
    def wants_markdown(self) -> bool:
        text = self.caps.get("text")
        return bool(isinstance(text, dict) and text.get("markdown"))

    @property
    def accepts_files(self) -> bool:
        return bool(self.caps.get("files"))

    def describe(self) -> dict:
        return {"device_id": self.device_id, "name": self.name, "kind": self.kind, "sdk": self.sdk,
                "paired": self.paired, "caps": self.caps, "actions": self.actions,
                "sensors": self.sensors, "connected_at": self.connected_at}

    def prompt_context(self) -> str:
        """Stable per-device text for the model: what this device is and can do."""
        parts = [f"This hermlet is '{self.name}' ({self.kind})."]
        display = self.caps.get("display")
        if isinstance(display, dict) and display.get("width"):
            parts.append(f"Its screen is {display['width']}x{display.get('height', '?')} pixels.")
        parts.append("It has a microphone." if self.has_mic else "It has no microphone.")
        if self.has_speaker:
            parts.append("It has a speaker.")
        if self.actions:
            names = ", ".join(a["name"] for a in self.actions[:24])
            parts.append(f"Device actions you can call with hermlet_action: {names}.")
        return " ".join(parts)

    # -- sending ------------------------------------------------------------------

    async def send_json(self, text: str) -> None:
        if self.closed:
            return
        async with self._send_lock:
            await self._ws.send(text)

    async def send_binary(self, frame: bytes) -> None:
        if self.closed:
            return
        async with self._send_lock:
            await self._ws.send(frame)

    async def emit(self, type_: str, **fields: Any) -> None:
        try:
            await self.send_json(protocol.message(type_, **fields))
        except Exception as exc:
            log.debug("send %s to %s failed: %s", type_, self.device_id, exc)

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def set_paired(self, paired: bool) -> None:
        self.paired = paired
        await self.emit("paired" if paired else "unpaired")

    async def send_pairing(self, code: str, command: str) -> None:
        await self.emit("pairing", code=code, command=command)

    async def send_notice(self, text: str, ttl_s: float = 8) -> None:
        await self.emit("notice", text=text, ttl_s=ttl_s)

    async def send_card(self, title: str, body: str, ttl_s: float = 20) -> None:
        await self.emit("card", title=title, body=body, ttl_s=ttl_s)

    def next_stream(self) -> int:
        self._next_stream = (self._next_stream + 1) & 0xFF
        return self._next_stream

    async def send_file(self, name: str, mime: str, data: bytes, caption: str | None = None,
                        turn: str | None = None) -> None:
        """Deliver a file (an image, a document) for the device to show or keep."""
        stream = self.next_stream()
        await self.send_json(protocol.message("file.start", stream=stream, name=name, mime=mime,
                                              size=len(data), caption=caption, turn=turn))
        for seq, start in enumerate(range(0, len(data), protocol.FILE_CHUNK_BYTES)):
            chunk = data[start:start + protocol.FILE_CHUNK_BYTES]
            await self.send_binary(protocol.binary(protocol.CHANNEL_FILE, stream, seq, chunk))
        await self.send_json(protocol.message("file.end", stream=stream, sha256=hashlib.sha256(data).hexdigest()))

    async def open_audio(self, rate: int, channels: int = 1, *, turn: str | None = None) -> AudioOut:
        if self._audio_out and not self._audio_out.done.is_set():
            await self._audio_out.abort()
        out = AudioOut(self, self.next_stream(), rate, channels)
        self._audio_out = out
        await out.start(turn=turn)
        return out

    async def play_wav(self, wav: bytes, *, turn: str | None = None) -> None:
        with wave.open(io.BytesIO(wav), "rb") as w:
            if w.getsampwidth() != 2:
                raise ValueError("only 16-bit WAV audio can be played")
            rate, channels, pcm = w.getframerate(), w.getnchannels(), w.readframes(w.getnframes())
        out = await self.open_audio(rate, channels, turn=turn)
        out.write(pcm)
        out.finish()

    async def invoke(self, name: str, args: dict | None = None, timeout: float = ACTION_TIMEOUT_S) -> Any:
        """Run a device action and return its result; raises ``ActionError``."""
        if not any(a["name"] == name for a in self.actions):
            raise ActionError(f"{self.name} has no action named {name!r}")
        call_id = uuid.uuid4().hex[:8]
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_actions[call_id] = future
        try:
            await self.send_json(protocol.message("action", id=call_id, name=name, args=args or {}))
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise ActionError(f"{self.name} did not answer {name} within {timeout:.0f}s") from None
        finally:
            self._pending_actions.pop(call_id, None)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        for task in list(self._tasks):
            task.cancel()
        for future in self._pending_actions.values():
            if not future.done():
                future.set_exception(ActionError("device disconnected"))
        try:
            await self._ws.close(code, reason)
        except Exception:
            pass

    # -- receiving ----------------------------------------------------------------

    async def handle_text(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            await self.emit("error", code="bad_json", message="not valid JSON")
            return
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            return
        kind = msg["type"]
        delegate = self.hub.delegate
        if kind == "actions":
            self.actions = _clean_actions(msg.get("actions"))
            return
        if kind == "action.result":
            future = self._pending_actions.get(str(msg.get("id")))
            if future and not future.done():
                if msg.get("ok"):
                    future.set_result(msg.get("result"))
                else:
                    future.set_exception(ActionError(str(msg.get("error") or "the action failed")))
            return
        if kind == "state":
            sensors = msg.get("sensors")
            if isinstance(sensors, dict):
                self.sensors.update(sensors)
                self.sensors_at = time.time()
            return
        if not self.paired:
            await self.emit("error", code="not_paired", message="approve this device on your Hermes first")
            return
        msg_id = str(msg.get("id") or uuid.uuid4().hex[:8])[:40]
        if kind == "text":
            await self._on_text(msg_id, msg)
        elif kind == "upload.start":
            await self._begin(msg_id, msg, "file")
        elif kind == "upload.end":
            await self._end_upload(msg_id, msg)
        elif kind == "audio.start":
            await self._begin(msg_id, msg, "audio")
        elif kind == "audio.end":
            await self._end_audio(msg_id, msg)
        elif kind in ("audio.cancel", "upload.cancel"):
            self._incoming.pop(_stream_of(msg), None)
            self._uploads.pop(msg_id, None)
        elif kind == "cancel":
            if self._audio_out:
                await self._audio_out.abort()
            await delegate.on_cancel(self)
        elif kind == "session.new":
            await delegate.on_new_session(self)
        elif kind == "prompt.reply":
            await delegate.on_prompt_reply(self, msg_id, str(msg.get("answer") or ""))
        elif kind == "event":
            self.spawn(delegate.on_event(self, str(msg.get("name") or "event")[:64], msg.get("data"), bool(msg.get("notify"))))
        elif kind in ("sessions.list", "sessions.get", "sessions.resume"):
            self.spawn(self._sessions(kind.split(".", 1)[1], msg_id, msg))
        elif kind == "persona.set":
            self.spawn(self._persona_set(msg_id, msg))
        elif kind == "look.set":
            self.spawn(self._look_set(msg_id, msg))

    async def _on_text(self, msg_id: str, msg: dict) -> None:
        text = str(msg.get("text") or "")
        wanted = [str(i) for i in (msg.get("attachments") or [])][:MAX_PENDING_UPLOADS]
        files = [self._uploads.pop(i) for i in wanted if i in self._uploads]
        if len(files) != len(wanted):
            await self.emit("error", code="missing_upload", id=msg_id, message="an attachment did not arrive; send it again")
            return
        if text.strip() or files:
            # Never block the receive loop on the delegate: it may call back into this
            # device (an action) and wait for an answer that arrives on this same loop.
            self.spawn(self._guard(self.hub.delegate.on_message(self, msg_id, text, files)))

    async def _guard(self, coro) -> None:
        """Run a delegate call; if it breaks, the device hears about it instead of waiting forever."""
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("handling a message from %s failed", self.device_id)
            await self.send_notice("Hermes could not take that message; see its log", ttl_s=15)
            await self.emit("turn.end", outcome="failure")

    async def _begin(self, msg_id: str, msg: dict, kind: str) -> None:
        stream = _stream_of(msg)
        if kind == "file":
            size = int(msg.get("size") or 0)
            if size > self.hub.max_upload_bytes or len(self._uploads) >= MAX_PENDING_UPLOADS:
                limit_mb = self.hub.max_upload_bytes / 1e6
                await self.emit("upload.error", id=msg_id, error=f"too large or too many files (limit {limit_mb:.0f} MB)")
                return
        self._incoming[stream] = _Incoming(
            id=msg_id, stream=stream, kind=kind, started=time.monotonic(),
            name=str(msg.get("name") or "file")[:200], mime=str(msg.get("mime") or "")[:100],
            size=int(msg.get("size") or 0), rate=int(msg.get("rate") or 16000))

    def handle_binary(self, frame: bytes) -> None:
        try:
            channel, stream, _seq, payload = protocol.parse_binary(frame)
        except ValueError:
            return
        incoming = self._incoming.get(stream)
        if incoming is None:
            return
        expected = "audio" if channel == protocol.CHANNEL_AUDIO else "file"
        if incoming.kind != expected or incoming.overflow:
            return
        limit = (self.hub.max_upload_bytes if incoming.kind == "file"
                 else int(self.hub.max_utterance_s * incoming.rate * 2))
        if len(incoming.data) + len(payload) > limit:
            incoming.overflow = incoming.kind == "file"
            payload = payload[:max(0, limit - len(incoming.data))]
        incoming.data += payload

    async def _end_upload(self, msg_id: str, msg: dict) -> None:
        incoming = self._incoming.pop(_stream_of(msg), None)
        if incoming is None or incoming.kind != "file":
            return
        digest = hashlib.sha256(incoming.data).hexdigest()
        claimed = str(msg.get("sha256") or "")
        if incoming.overflow or (claimed and claimed != digest):
            await self.emit("upload.error", id=incoming.id, error="the file did not arrive intact")
            return
        self._uploads[incoming.id] = Attachment(incoming.name, incoming.mime, bytes(incoming.data))
        await self.emit("upload.ok", id=incoming.id, size=len(incoming.data))

    async def _end_audio(self, msg_id: str, msg: dict) -> None:
        incoming = self._incoming.pop(_stream_of(msg), None)
        if incoming is None or incoming.kind != "audio":
            return
        seconds = len(incoming.data) / 2 / max(1, incoming.rate)
        if seconds < MIN_UTTERANCE_S:
            await self.send_notice("That was too short to hear")
            return
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(incoming.rate)
            w.writeframes(bytes(incoming.data))
        self.spawn(self._guard(self.hub.delegate.on_utterance(self, incoming.id, buf.getvalue(), seconds)))

    async def _persona_set(self, msg_id: str, msg: dict) -> None:
        try:
            result = await self.hub.delegate.on_persona_set(self, msg.get("name"), msg.get("colour"))
        except Exception as exc:
            log.warning("persona.set from %s failed: %s", self.device_id, exc)
            result = {"error": str(exc) or exc.__class__.__name__}
        await self.emit("persona.result", id=msg_id, **result)
        if not result.get("error"):
            await self.hub.broadcast_persona()  # every other device of this agent follows

    async def _look_set(self, msg_id: str, msg: dict) -> None:
        changes = msg.get("changes") if isinstance(msg.get("changes"), dict) else {}
        try:
            result = await self.hub.delegate.on_look_set(self, changes, msg.get("device") or None)
        except Exception as exc:
            log.warning("look.set from %s failed: %s", self.device_id, exc)
            result = {"error": str(exc) or exc.__class__.__name__}
        await self.emit("look.result", id=msg_id, **result)
        if not result.get("error"):
            await self.hub.broadcast_look()

    async def _sessions(self, op: str, msg_id: str, msg: dict) -> None:
        try:
            result = await self.hub.delegate.on_sessions(self, op, msg)
        except Exception as exc:
            log.warning("sessions.%s for %s failed: %s", op, self.device_id, exc)
            result = {"error": str(exc) or exc.__class__.__name__}
        await self.emit("sessions.result", id=msg_id, op=op, **result)


def _subprotocol(connection, offered) -> str | None:
    """Agree on ours when a client offers it; clients that offer none are still welcome."""
    return protocol.SUBPROTOCOL if protocol.SUBPROTOCOL in offered else None


def _stream_of(msg: dict) -> int:
    try:
        return int(msg.get("stream") or 0) & 0xFF
    except (TypeError, ValueError):
        return 0


def _clean_actions(raw: Any) -> list[dict]:
    out = []
    for a in raw if isinstance(raw, list) else []:
        if isinstance(a, dict) and isinstance(a.get("name"), str) and a["name"]:
            out.append({"name": a["name"][:64], "description": str(a.get("description") or "")[:400],
                        "params": a.get("params") if isinstance(a.get("params"), dict) else {"type": "object", "properties": {}}})
    return out[:64]


class DeviceHub:
    def __init__(self, store: DeviceStore, delegate: HubDelegate, *, host: str = "0.0.0.0",
                 port: int = protocol.DEFAULT_PORT, path: str = protocol.DEFAULT_PATH,
                 access_token: str | None = None, heartbeat_s: int = 20, ssl_context=None,
                 max_utterance_s: float = 120, max_upload_mb: float = 25):
        self.store = store
        self.delegate = delegate
        self.host, self.port, self.path = host, port, path
        self.access_token = access_token or None
        self.heartbeat_s = heartbeat_s
        self.ssl_context = ssl_context
        self.max_utterance_s = max_utterance_s
        self.max_upload_bytes = int(max_upload_mb * 1e6)
        self.sessions: dict[str, DeviceSession] = {}
        self._server = None
        self.bound_port = port

    async def start(self) -> None:
        from websockets.asyncio.server import serve

        self._server = await serve(
            self._handle, self.host, self.port, ssl=self.ssl_context, select_subprotocol=_subprotocol,
            max_size=protocol.MAX_FRAME_BYTES, ping_interval=self.heartbeat_s, ping_timeout=self.heartbeat_s * 2,
            process_request=self._http)
        self.bound_port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for session in list(self.sessions.values()):
            await session.close(1001, "hub stopping")
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def get(self, device_id: str) -> DeviceSession | None:
        return self.sessions.get(device_id)

    def find(self, ref: str | None) -> DeviceSession | None:
        """A session by device id or (case-insensitive) name; the only one when ``ref`` is empty."""
        if not ref:
            return next(iter(self.sessions.values())) if len(self.sessions) == 1 else None
        if ref in self.sessions:
            return self.sessions[ref]
        low = ref.strip().lower()
        matches = [s for s in self.sessions.values() if s.name.lower() == low]
        return matches[0] if len(matches) == 1 else None

    async def broadcast_persona(self) -> None:
        persona = self.delegate.persona()
        for session in list(self.sessions.values()):
            await session.emit("persona", persona=persona)

    async def broadcast_look(self) -> None:
        look = self.delegate.look()
        for session in list(self.sessions.values()):
            await session.emit("look", look=look)

    def _http(self, connection, request):
        """Plain HTTP on the hub port: a health check, so setup tools can tell it is reachable."""
        if request.path.split("?")[0] == self.path and "upgrade" in (request.headers.get("Connection") or "").lower():
            return None
        if request.path.split("?")[0] in ("/", "/health", self.path):
            return connection.respond(200, f"hermlet hub ok (proto {protocol.PROTO})\n")
        return connection.respond(404, "not found\n")

    async def _handshake(self, ws) -> DeviceSession:
        raw = await asyncio.wait_for(ws.recv(), HANDSHAKE_TIMEOUT_S)
        try:
            hello = json.loads(raw)
        except (ValueError, TypeError):
            raise Rejected("bad_hello", "the first message must be a hello") from None
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            raise Rejected("bad_hello", "the first message must be a hello")
        if int(hello.get("proto") or 0) != protocol.PROTO:
            raise Rejected("bad_proto", f"this hub speaks protocol {protocol.PROTO}")
        device_id = str(hello.get("device_id") or "")
        if not protocol.DEVICE_ID_RE.match(device_id):
            raise Rejected("bad_device_id", "device_id must look like hl-0123456789abcdef")
        if self.access_token and not hmac.compare_digest(str(hello.get("token") or ""), self.access_token):
            raise Rejected("bad_token", "the access token does not match this Hermes")
        key = self.store.key_for(device_id)
        nonce = base64.b64encode(secrets.token_bytes(16)).decode()
        await ws.send(protocol.message("challenge", nonce=nonce, enrolled=key is not None))
        try:
            auth = json.loads(await asyncio.wait_for(ws.recv(), HANDSHAKE_TIMEOUT_S))
        except (ValueError, TypeError):
            raise Rejected("auth_failed", "expected an auth message") from None
        if not isinstance(auth, dict) or auth.get("type") != "auth":
            raise Rejected("auth_failed", "expected an auth message")
        name, kind = str(hello.get("name") or "")[:64], str(hello.get("kind") or "")[:64]
        if key is None:
            try:
                key = base64.b64decode(str(auth.get("key") or ""), validate=True)
            except ValueError:
                key = b""
            if len(key) != protocol.KEY_BYTES or protocol.device_id_for(key) != device_id:
                raise Rejected("auth_failed", "the key does not belong to this device id")
            self.store.enroll(device_id, key, name=name, kind=kind)
            log.info("enrolled new device %s (%s)", device_id, name)
        else:
            if not hmac.compare_digest(str(auth.get("mac") or ""), protocol.mac_for(key, device_id, nonce)):
                raise Rejected("auth_failed", "this device could not prove its identity; "
                                              f"if it was reset, run 'hermes hermlet forget {device_id}'")
            self.store.touch(device_id, name=name, kind=kind)
        return DeviceSession(self, ws, hello)

    async def _handle(self, ws) -> None:
        try:
            session = await self._handshake(ws)
        except Rejected as exc:
            try:
                await ws.send(protocol.message("error", code=exc.code, message=exc.message))
                await ws.close(1008, exc.code)
            except Exception:
                pass
            return
        except (asyncio.TimeoutError, Exception) as exc:
            log.debug("handshake failed: %s", exc)
            return
        previous = self.sessions.get(session.device_id)
        if previous is not None:
            await previous.close(1000, "replaced by a new connection")
        self.sessions[session.device_id] = session
        try:
            session.paired = bool(await self.delegate.is_paired(session))
            await session.send_json(protocol.message(
                "welcome", proto=protocol.PROTO, session=session.session_id, paired=session.paired,
                heartbeat_s=self.heartbeat_s, agent=self.delegate.agent_name(), persona=self.delegate.persona(),
                look=self.delegate.look(),
                limits={"upload_bytes": self.max_upload_bytes, "utterance_s": self.max_utterance_s}))
            await self.delegate.on_ready(session)
            async for frame in ws:
                if isinstance(frame, (bytes, bytearray)):
                    if session.paired:
                        session.handle_binary(bytes(frame))
                else:
                    await session.handle_text(frame)
        except Exception as exc:
            log.debug("connection to %s ended: %s", session.device_id, exc)
        finally:
            session.closed = True
            if self.sessions.get(session.device_id) is session:
                del self.sessions[session.device_id]
            for task in list(session._tasks):
                task.cancel()
            for future in session._pending_actions.values():
                if not future.done():
                    future.set_exception(ActionError("device disconnected"))
            try:
                await self.delegate.on_disconnect(session)
            except Exception:
                log.debug("on_disconnect failed", exc_info=True)
