"""Shape agent text for a device that shows plain text in a chat bubble."""

from __future__ import annotations

import re

_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", re.DOTALL)
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"(\*\*|__)(.+?)\1", re.DOTALL)
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_BULLET = re.compile(r"^(\s*)[*+-]\s+", re.MULTILINE)


def plain(text: str) -> str:
    """Markdown reduced to readable plain text; code and URLs are kept."""
    if not text:
        return ""
    out = _FENCE.sub(lambda m: m.group(1).strip("\n"), text)
    out = _LINK.sub(lambda m: m.group(1) if m.group(1) == m.group(2) else f"{m.group(1)} ({m.group(2)})", out)
    out = _BOLD.sub(lambda m: m.group(2), out)
    out = _ITALIC.sub(lambda m: m.group(1), out)
    out = _HEADING.sub("", out)
    out = _BULLET.sub(lambda m: f"{m.group(1)}- ", out)
    out = out.replace("`", "")
    return re.sub(r"\n{3,}", "\n\n", out).strip()
