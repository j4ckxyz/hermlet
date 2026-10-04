"""Where a hermlet keeps its things, and its settings.

Everything lives under one directory: ``$HERMLET_HOME`` or
``~/.config/hermlet``. Settings are a small JSON file so nothing beyond the
standard library is needed to read them, including from the native app.

    config.json     address of your Hermes, device name, audio settings
    auth.json       the signed-in session with your Hermes (mode 0600; never the password)
    session.json    which conversation this device is in
    identity.json   plugin mode only: this device's secret key (mode 0600)
    persona.json    the agent's critter, cached from the last connection
    hermlet.sock    the local socket apps talk to while ``hermlet run`` is up
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULTS: Dict[str, Any] = {
    "url": "",              # https://your-hermes.tailnet.ts.net (the Hermes dashboard)
    "mode": "",             # dashboard (Hermes's own web API) or plugin; empty: decided by the address
    "name": "",             # shown to you and to the agent; defaults to the host name
    "token": "",            # only if your Hermes sets HERMLET_ACCESS_TOKEN
    "speak": False,         # play spoken replies when Hermes sends them
    "mic": "",              # capture device, e.g. plughw:1,0 (empty: system default)
    "speaker": "",          # playback device (empty: system default)
    "mic_rate": 16000,
    "inbox": "",            # where files from Hermes are saved; defaults to ~/Hermlet
    "theme": "auto",        # auto (light by day, dark at night), light or dark
}


def home() -> Path:
    override = os.environ.get("HERMLET_HOME")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "hermlet"


def socket_path(base: Optional[Path] = None) -> Path:
    return (base or home()) / "hermlet.sock"


def inbox(cfg: Dict[str, Any]) -> Path:
    return Path(cfg.get("inbox") or os.path.join(os.path.expanduser("~"), "Hermlet")).expanduser()


def default_name() -> str:
    host = socket.gethostname().split(".")[0] or "hermlet"
    return host[:1].upper() + host[1:]


def load(base: Optional[Path] = None) -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads(((base or home()) / "config.json").read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            cfg.update(raw)
    except (OSError, ValueError):
        pass
    if not cfg.get("name"):
        cfg["name"] = default_name()
    return cfg


def save(cfg: Dict[str, Any], base: Optional[Path] = None) -> Path:
    directory = base or home()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.json"
    write_private(path, json.dumps(cfg, indent=2, sort_keys=True) + "\n")
    return path


def write_private(path: Path, text: str) -> None:
    """Write a file only this account can read, replacing it in one step."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
