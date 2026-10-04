"""Hermlet wire protocol, version 1: constants and framing shared by the hub.

The device side keeps its own copy in ``src/hermlet/protocol.py``; a test pins
the two files to the same values. See ``docs/protocol.md`` for the contract.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import struct
from typing import Any

PROTO = 1
SUBPROTOCOL = "hermlet.v1"
DEFAULT_PORT = 8790
DEFAULT_PATH = "/hermlet"

DEVICE_ID_RE = re.compile(r"^hl-[0-9a-f]{16}$")
KEY_BYTES = 32
MAC_CONTEXT = "hermlet/v1"

# Binary frames: channel u8, stream u8, sequence u16 LE, then the payload.
CHANNEL_AUDIO = 0x01   # mono PCM16 LE at the rate announced by audio.start
CHANNEL_FILE = 0x02    # the next bytes of an upload (device -> host) or a file (host -> device)
HEADER = struct.Struct("<BBH")
FILE_CHUNK_BYTES = 32 * 1024
MAX_FRAME_BYTES = 1 << 20


def device_id_for(key: bytes) -> str:
    return "hl-" + hashlib.sha256(key).hexdigest()[:16]


def mac_for(key: bytes, device_id: str, nonce: str) -> str:
    """The answer to a challenge: base64 HMAC-SHA256 over the device id and nonce."""
    data = f"{MAC_CONTEXT}|{device_id}|{nonce}".encode()
    return base64.b64encode(hmac.new(key, data, hashlib.sha256).digest()).decode()


def message(type_: str, **fields: Any) -> str:
    """A JSON text frame. Fields that are None are left out, never sent as null."""
    body = {"type": type_}
    body.update({k: v for k, v in fields.items() if v is not None})
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False)


def binary(channel: int, stream: int, seq: int, payload: bytes) -> bytes:
    return HEADER.pack(channel, stream & 0xFF, seq & 0xFFFF) + payload


def parse_binary(frame: bytes) -> tuple[int, int, int, bytes]:
    if len(frame) < HEADER.size:
        raise ValueError("binary frame shorter than its header")
    channel, stream, seq = HEADER.unpack_from(frame)
    return channel, stream, seq, frame[HEADER.size:]
