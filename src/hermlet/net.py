"""Finding Hermes by its Tailscale name even when this machine does not use Tailscale's DNS.

Plenty of devices join a tailnet with MagicDNS turned off (``--accept-dns=false``), so
``hermes.your-tailnet.ts.net`` does not resolve on them even though the machine is one hop
away. When an ordinary lookup fails, this asks the local ``tailscale`` for that peer's address
instead. TLS still checks the certificate against the name, so nothing is weakened.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from typing import Any, Dict, Optional

_original = socket.getaddrinfo
_cache: Dict[str, Any] = {"at": 0.0, "names": {}}
_installed = False
used: Dict[str, str] = {}  # names this process had to resolve through Tailscale, for `hermlet doctor`


def peers(status: Dict[str, Any]) -> Dict[str, str]:
    """``tailscale status --json`` as {dns name (and short name): first address}."""
    names: Dict[str, str] = {}
    for peer in list((status.get("Peer") or {}).values()) + [status.get("Self") or {}]:
        addresses = peer.get("TailscaleIPs") or []
        dns = str(peer.get("DNSName") or "").rstrip(".").lower()
        if not addresses or not dns:
            continue
        names[dns] = addresses[0]
        names.setdefault(dns.split(".", 1)[0], addresses[0])
    return names


def tailscale_address(host: str) -> Optional[str]:
    """The tailnet address of ``host``, or None when Tailscale is absent or does not know it."""
    now = time.time()
    if now - _cache["at"] > 30:
        _cache["at"], _cache["names"] = now, {}
        binary = shutil.which("tailscale")
        if binary:
            try:
                done = subprocess.run([binary, "status", "--json"], capture_output=True, text=True, timeout=5)
                _cache["names"] = peers(json.loads(done.stdout or "{}"))
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
    return _cache["names"].get(host.rstrip(".").lower())


def _getaddrinfo(host, port, *args, **kwargs):
    try:
        return _original(host, port, *args, **kwargs)
    except socket.gaierror:
        address = tailscale_address(host) if isinstance(host, str) else None
        if not address:
            raise
        used[host] = address
        return _original(address, port, *args, **kwargs)


def install() -> None:
    """Make name lookups in this process fall back to Tailscale. Safe to call more than once."""
    global _installed
    if not _installed:
        socket.getaddrinfo = _getaddrinfo
        _installed = True
