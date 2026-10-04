"""This device's identity: a random key made on first run, and the id derived from it.

The key is enrolled with a Hermes the first time the device connects to it and
is never sent again: later connections answer a challenge with an HMAC. The
identity remembers which hubs it has enrolled with, so a hub that claims not
to know the device cannot talk it into revealing the key a second time.
"""

from __future__ import annotations

import base64
import json
import secrets
from pathlib import Path
from typing import List, Optional

from . import config, protocol


class Identity:
    def __init__(self, key: bytes, enrolled: Optional[List[str]] = None, path: Optional[Path] = None):
        self.key = key
        self.device_id = protocol.device_id_for(key)
        self.enrolled = list(enrolled or [])
        self._path = path

    @classmethod
    def load(cls, base: Optional[Path] = None) -> "Identity":
        path = (base or config.home()) / "identity.json"
        raw = config.read_json(path)
        if isinstance(raw, dict):
            try:
                key = base64.b64decode(raw["key"])
                if len(key) == protocol.KEY_BYTES:
                    return cls(key, raw.get("enrolled"), path)
            except (KeyError, ValueError):
                pass
        identity = cls(secrets.token_bytes(protocol.KEY_BYTES), [], path)
        identity.save()
        return identity

    def save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        config.write_private(self._path, json.dumps(
            {"device_id": self.device_id, "key": base64.b64encode(self.key).decode(), "enrolled": self.enrolled},
            indent=2) + "\n")

    def is_enrolled(self, url: str) -> bool:
        return url in self.enrolled

    def mark_enrolled(self, url: str) -> None:
        if url not in self.enrolled:
            self.enrolled.append(url)
            self.save()

    def forget_enrollment(self, url: Optional[str] = None) -> None:
        self.enrolled = [u for u in self.enrolled if url and u != url]
        self.save()
