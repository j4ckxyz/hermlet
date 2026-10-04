"""Microphone capture and speaker playback through the system's own tools.

No audio library is loaded into Python: a recorder or player is a small child
process (``arecord``/``aplay`` from alsa-utils on a Raspberry Pi, PipeWire's
``pw-record``/``pw-play`` or ``sox`` elsewhere) streaming raw PCM16 over a
pipe. That keeps a hermlet light and works on every Linux audio stack.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import struct
import subprocess
import tempfile
from typing import AsyncIterator, List, Optional

CHUNK_MS = 60


def _recorders(rate: int, device: str) -> List[List[str]]:
    dev = ["-D", device] if device else []
    return [
        ["arecord", "-q", *dev, "-f", "S16_LE", "-c", "1", "-r", str(rate), "-t", "raw"],
        ["pw-record", "--format", "s16", "--channels", "1", "--rate", str(rate), "-"],
        ["parecord", "--raw", "--format=s16le", "--channels=1", f"--rate={rate}"],
        ["rec", "-q", "-t", "raw", "-b", "16", "-e", "signed", "-c", "1", "-r", str(rate), "-"],
    ]


def _players(rate: int, channels: int, device: str) -> List[List[str]]:
    dev = ["-D", device] if device else []
    return [
        ["aplay", "-q", *dev, "-f", "S16_LE", "-c", str(channels), "-r", str(rate), "-t", "raw"],
        ["pw-play", "--format", "s16", "--channels", str(channels), "--rate", str(rate), "-"],
        ["paplay", "--raw", "--format=s16le", f"--channels={channels}", f"--rate={rate}"],
        ["play", "-q", "-t", "raw", "-b", "16", "-e", "signed", "-c", str(channels), "-r", str(rate), "-"],
    ]


def _first_available(candidates: List[List[str]]) -> Optional[List[str]]:
    for command in candidates:
        if shutil.which(command[0]):
            return command
    return None


def can_record() -> bool:
    """Whether there is a recording tool and something for it to record from."""
    command = _first_available(_recorders(16000, ""))
    if command is None:
        return False
    if command[0] == "arecord":  # the tool is on every Raspberry Pi; a microphone is not
        try:
            listing = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return "card " in listing
    return True


def can_play() -> bool:
    return _first_available(_players(16000, 1, "")) is not None


def can_play_files() -> bool:
    """Whether a finished audio file (the form Hermes's text-to-speech returns) can be played."""
    return _first_available([list(c) for c in _FILE_PLAYERS]) is not None


def level(pcm: bytes) -> float:
    """Loudness of a PCM16 chunk from 0 to 1, for a microphone meter."""
    count = len(pcm) // 2
    if not count:
        return 0.0
    step = max(1, count // 200)
    peak = max(abs(struct.unpack_from("<h", pcm, i * 2)[0]) for i in range(0, count, step))
    return min(1.0, peak / 32768.0)


class Recorder:
    """``async for chunk in recorder`` yields PCM16 mono until ``stop()``."""

    def __init__(self, rate: int = 16000, device: str = ""):
        self.rate = rate
        self._command = _first_available(_recorders(rate, device))
        self._proc: Optional[asyncio.subprocess.Process] = None

    @property
    def available(self) -> bool:
        return self._command is not None

    async def start(self) -> None:
        if self._command is None:
            raise RuntimeError("no recording tool found; install alsa-utils (arecord)")
        self._proc = await asyncio.create_subprocess_exec(
            *self._command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

    async def chunks(self) -> AsyncIterator[bytes]:
        assert self._proc is not None and self._proc.stdout is not None
        size = self.rate * 2 * CHUNK_MS // 1000
        while True:
            try:
                chunk = await self._proc.stdout.readexactly(size)
            except asyncio.IncompleteReadError as exc:
                if exc.partial:
                    yield exc.partial[:len(exc.partial) // 2 * 2]
                return
            yield chunk

    async def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 2)
        except asyncio.TimeoutError:
            proc.kill()


class Player:
    """Plays one PCM16 stream: ``open``, ``write`` chunks, then ``close`` (or ``abort``)."""

    def __init__(self, device: str = ""):
        self._device = device
        self._proc: Optional[asyncio.subprocess.Process] = None

    async def open(self, rate: int, channels: int = 1) -> bool:
        await self.abort()
        command = _first_available(_players(rate, channels, self._device))
        if command is None:
            return False
        self._proc = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        return True

    async def write(self, pcm: bytes) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            return
        try:
            proc.stdin.write(pcm)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            self._proc = None

    async def close(self) -> None:
        """Let what was written finish playing."""
        proc, self._proc = self._proc, None
        if proc is None or proc.stdin is None:
            return
        try:
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), 30)
        except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError):
            proc.kill()

    async def abort(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            proc.kill()
            try:
                await proc.wait()
            except ProcessLookupError:
                pass


_FILE_PLAYERS = (
    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"],
    ["mpv", "--no-video", "--really-quiet"],
    ["mpg123", "-q"],
    ["cvlc", "--play-and-exit", "--quiet"],
    ["afplay"],
)


async def play_encoded(data: bytes, mime: str = "") -> bool:
    """Play a finished audio file (MP3, WAV, OGG) with whichever player this machine has."""
    wav = data[:4] == b"RIFF"
    command = _first_available([["aplay", "-q"]] if wav else []) or _first_available([list(c) for c in _FILE_PLAYERS])
    if command is None or not data:
        return False
    suffix = ".wav" if wav else ".ogg" if "ogg" in mime or "opus" in mime else ".mp3"
    fd, path = tempfile.mkstemp(prefix="hermlet-", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        proc = await asyncio.create_subprocess_exec(
            *command, path, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        return proc.returncode == 0
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
