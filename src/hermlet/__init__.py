"""Hermlet: put your own Hermes agent on a Raspberry Pi (or any Linux box).

Two ways in, depending on what you are building:

``hermlet.connect()``
    Talk to the ``hermlet run`` service already on this machine. Any number of
    programs can share it. This is what apps use.

``hermlet.Device()``
    Be the device yourself: one asyncio object holding the connection to
    Hermes. For single-purpose gadgets, and what ``hermlet run`` is built on.
"""

from __future__ import annotations

__version__ = "0.1.0"

from pathlib import Path  # noqa: E402
from typing import Any, Optional, Union  # noqa: E402

from . import config  # noqa: E402
from .base import BaseDevice, HermletError, NeedsSignIn  # noqa: E402
from .client import Hermlet, connect  # noqa: E402
from .dashboard import DashboardDevice  # noqa: E402
from .device import HubDevice  # noqa: E402

__all__ = ["BaseDevice", "DashboardDevice", "Device", "Hermlet", "HermletError", "HubDevice", "NeedsSignIn",
           "connect", "mode_for", "__version__"]


def mode_for(url: str, configured: str = "") -> str:
    """Which way a device reaches Hermes: ``dashboard`` (its own web API) or ``plugin`` (the hub)."""
    if configured in ("dashboard", "plugin"):
        return configured
    low = (url or "").strip().lower()
    if low.startswith(("ws://", "wss://")) or low.rstrip("/").endswith("/hermlet"):
        return "plugin"
    return "dashboard"


def Device(url: Optional[str] = None, name: Optional[str] = None, *, home: Optional[Union[str, Path]] = None,
           mode: Optional[str] = None, **kw: Any) -> BaseDevice:
    """A connection to your Hermes, by whichever route the address (or ``mode``) selects.

    By default a device uses Hermes's own web API, so nothing has to be
    installed on the Hermes side. An address that starts with ``ws://`` (or
    ``mode="plugin"``) uses the optional Hermlet plugin instead.
    """
    cfg = config.load(Path(home) if home else None)
    picked = mode_for(url or cfg.get("url") or "", mode or cfg.get("mode") or "")
    return (HubDevice if picked == "plugin" else DashboardDevice)(url, name, home=home, **kw)
