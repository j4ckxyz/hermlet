"""Actions every hermlet offers out of the box.

Only ``device.health`` is on by default: it reads, never changes anything.
Anything that lets the agent act on the machine (run commands, write files) is
left to you to add on purpose; ``examples/shell_action.py`` shows how.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict


def health() -> Dict[str, Any]:
    """Uptime, load, memory, disk and temperature of this machine."""
    out: Dict[str, Any] = {"time": time.strftime("%Y-%m-%d %H:%M:%S %Z")}
    try:
        out["load"] = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        pass
    try:
        out["uptime_s"] = int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        pass
    try:
        meminfo = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line)
        total, free = int(meminfo["MemTotal"].split()[0]), int(meminfo["MemAvailable"].split()[0])
        out["memory_mb"] = {"total": total // 1024, "available": free // 1024}
    except (OSError, KeyError, ValueError, IndexError):
        pass
    try:
        usage = shutil.disk_usage(os.path.expanduser("~"))
        out["disk_gb"] = {"total": round(usage.total / 1e9, 1), "free": round(usage.free / 1e9, 1)}
    except OSError:
        pass
    try:
        out["temperature_c"] = round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000, 1)
    except (OSError, ValueError):
        pass
    return out


def register(device) -> None:
    device.add_action("device.health", health, "Report this device's uptime, load, memory, disk space and temperature.")
