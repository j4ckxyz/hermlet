"""How the app looks and behaves: one small settings file, kept on Hermes.

Every hermlet of one Hermes reads the same ``look.json``, so a change made on
one device, in a terminal, or by the agent itself shows up on all of them
within seconds. The agent can make that change because of a skill
(``SKILL.md`` below) that tells it where the file is and what goes in it:
"make the accent red" or "use a code-style font on the kitchen Pi" then works
from any surface the agent is reachable on.

    {"version": 1,
     "theme": "auto",          auto (light by day, dark at night) | light | dark
     "accent": null,           "#rrggbb" or a swatch name; null follows the critter's colour
     "background": null,       "#rrggbb"; null follows the theme
     "surface": null,          "#rrggbb": cards, the prompt box
     "text": null,             "#rrggbb"
     "font": "rounded",        rounded | mono | serif | pixel | /path/to/a/font.ttf on the device
     "text_size": "medium",    small | medium | large | a pixel size from 10 to 40
     "corners": "round",       round | soft | square
     "fun": true,              the critter, pixel headings and little animations; false is plain
     "critter_size": "large",  small | medium | large
     "speak": false,           read replies aloud (needs text-to-speech on Hermes and a speaker)
     "greeting": null,         a line for the home screen; null lets the app choose
     "fresh_after_min": 20,    after this many idle minutes the next visit starts a fresh
                               conversation (short conversations cost less); 0 never does
     "devices": {"Kitchen Pi": {"text_size": "large"}}}     per-device overrides, by device name

No Hermes imports; the plugin keeps a byte-identical copy.
"""

from __future__ import annotations

import json
from typing import Any

VERSION = 1

DEFAULTS: dict[str, Any] = {
    "theme": "auto", "accent": None, "background": None, "surface": None, "text": None,
    "font": "rounded", "text_size": "medium", "corners": "round", "fun": True,
    "critter_size": "large", "speak": False, "greeting": None, "fresh_after_min": 20,
}
_CHOICES = {
    "theme": ("auto", "light", "dark"),
    "corners": ("round", "soft", "square"),
    "critter_size": ("small", "medium", "large"),
}
_FONTS = ("rounded", "mono", "serif", "pixel")
_SIZES = ("small", "medium", "large")
_COLOUR_KEYS = ("accent", "background", "surface", "text")
# Words people (and models) use for colours, beyond the critter swatches.
_COLOUR_WORDS = {
    "red": "#e5484d", "orange": "#f08c3c", "yellow": "#ecd75f", "green": "#5bb974", "cyan": "#4cc3d9",
    "purple": "#9b7fe6", "pink": "#ec7fa8", "white": "#ffffff", "black": "#111014", "grey": "#8d8794",
    "gray": "#8d8794", "moss": "#9ccb5a", "mint": "#6fd3a8", "teal": "#4fb3a6", "sky": "#69b7e8",
    "blue": "#6f8ff0", "lilac": "#a98fe6", "plum": "#c77fd6", "rose": "#ec7fa8", "coral": "#f08c6c",
    "amber": "#f2b24a", "butter": "#ecd75f", "stone": "#a9a39b",
}


def colour(value: Any) -> str | None:
    """A colour as ``#rrggbb``: accepts that form, ``#rgb``, or a colour word. None when it is neither."""
    text = str(value or "").strip().lower()
    if text in _COLOUR_WORDS:
        return _COLOUR_WORDS[text]
    if text.startswith("#") and all(c in "0123456789abcdef" for c in text[1:]):
        if len(text) == 7:
            return text
        if len(text) == 4:
            return "#" + "".join(c * 2 for c in text[1:])
    return None


def _clean_one(raw: Any) -> dict[str, Any]:
    """The recognised settings in ``raw``, each in its stored form. Anything else is dropped."""
    out: dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    for key, allowed in _CHOICES.items():
        if str(raw.get(key) or "").lower() in allowed:
            out[key] = str(raw[key]).lower()
    for key in _COLOUR_KEYS:
        if key in raw:
            out[key] = colour(raw[key])  # an unreadable colour clears the override
    font = raw.get("font")
    if isinstance(font, str) and font.strip():
        font = font.strip()
        low = font.lower()
        if low in _FONTS:
            out["font"] = low
        elif low in ("code", "monospace", "monospaced", "terminal"):
            out["font"] = "mono"
        elif low in ("sans", "default", "round"):
            out["font"] = "rounded"
        elif font.startswith("/") and low.endswith((".ttf", ".otf")) and len(font) < 300:
            out["font"] = font
    size = raw.get("text_size")
    if isinstance(size, str) and size.lower() in _SIZES:
        out["text_size"] = size.lower()
    elif isinstance(size, (int, float)) and not isinstance(size, bool) and 10 <= size <= 40:
        out["text_size"] = int(size)
    for key in ("fun", "speak"):
        if isinstance(raw.get(key), bool):
            out[key] = raw[key]
    fresh = raw.get("fresh_after_min")
    if isinstance(fresh, (int, float)) and not isinstance(fresh, bool) and 0 <= fresh <= 7 * 24 * 60:
        out["fresh_after_min"] = int(fresh)
    if "greeting" in raw:
        greeting = raw["greeting"]
        out["greeting"] = " ".join(str(greeting).split())[:80] if isinstance(greeting, str) and greeting.strip() else None
    return out


def clean(raw: Any) -> dict[str, Any]:
    """A whole look file made safe to store: known keys only, plus per-device overrides."""
    out = _clean_one(raw)
    devices = raw.get("devices") if isinstance(raw, dict) else None
    if isinstance(devices, dict):
        cleaned = {str(name)[:64]: _clean_one(value) for name, value in list(devices.items())[:32]}
        cleaned = {name: value for name, value in cleaned.items() if value}
        if cleaned:
            out["devices"] = cleaned
    out["version"] = VERSION
    return out


def parse(text: Any) -> dict[str, Any]:
    """A look file's text as a cleaned look. Unreadable text is an empty look, never an error."""
    try:
        return clean(json.loads(text)) if isinstance(text, str) and text.strip() else clean({})
    except ValueError:
        return clean({})


def resolve(look: dict[str, Any], device_name: str = "") -> dict[str, Any]:
    """What one device should show: defaults, then the shared look, then that device's overrides."""
    effective = dict(DEFAULTS)
    effective.update({k: v for k, v in look.items() if k in DEFAULTS})
    overrides = (look.get("devices") or {}).get(device_name) or {}
    effective.update({k: v for k, v in overrides.items() if k in DEFAULTS})
    return effective


def merged(look: dict[str, Any], changes: dict[str, Any], device_name: str | None = None) -> dict[str, Any]:
    """``look`` with ``changes`` applied, for every device or (with a name) for one."""
    updated = clean(look)
    wanted = _clean_one(changes)
    if device_name:
        devices = dict(updated.get("devices") or {})
        devices[device_name] = {**(devices.get(device_name) or {}), **wanted}
        updated["devices"] = devices
    else:
        updated.update(wanted)
    return clean(updated)


def dumps(look: dict[str, Any]) -> str:
    return json.dumps(clean(look), indent=1) + "\n"


SKILL_NAME = "hermlet"
CONTEXT_MARK = "[Hermlet device context"


def context(device_name: str, critter_name: str, look_path: str) -> str:
    """What the agent must know at the start of a conversation held on a device.

    Without it "make the accent red" is ambiguous, and an agent will happily restyle
    Hermes itself. This says where the conversation is happening and which file the
    request means, in few enough words to send with every new conversation.
    """
    who = f"; you appear there as a pixel critter named {critter_name}" if critter_name else ""
    return (
        f"{CONTEXT_MARK}; not typed by the user] This conversation is on the user's Hermlet device "
        f"\"{device_name}\", a small screen running the Hermlet app{who}. A request here to change the "
        f"look (theme, colours, accent, font, text size, plain or playful) means that app: load the "
        f"'{SKILL_NAME}' skill and edit {look_path}. Never change Hermes's own skin or config for it.")


def skill(look_path: str) -> str:
    """The SKILL.md that teaches the agent to restyle the app. ``look_path`` is where the file lives."""
    return f"""---
name: {SKILL_NAME}
description: Change how the user's Hermlet devices (small screens that talk to you) look and behave - colours, font, text size, the critter, spoken replies - by editing one JSON file. This is the Hermlet app's look, not Hermes's own skin.
version: 1.0.0
metadata:
  hermes:
    tags: [Hermlet, Devices, Appearance]
---

# Restyling the Hermlet app

The user has one or more Hermlet devices: small screens (often a Raspberry Pi) running an app
that talks to you. How that app looks is one JSON file on this machine:

    {look_path}

Every device re-reads it within a few seconds, and immediately after you finish a tool call in a
conversation with a device, so a change shows up while the user is watching.

## When to use

The user asks to change the app's look or behaviour: "make the accent red", "use a code-style
font", "bigger text", "turn off the cute stuff", "dark mode", "read your answers out loud",
"make it look professional", "change it back".

## Procedure

1. Read the file (it may not exist yet; treat that as `{{}}`).
2. Change only the keys the user asked about. Keep everything else, including `devices`.
3. Write the whole file back as valid JSON.
4. Tell the user in one short sentence what changed. Do not paste the JSON.

## Keys

| Key | Values | Meaning |
|---|---|---|
| `theme` | `"auto"`, `"light"`, `"dark"` | `auto` is light by day, dark in the evening |
| `accent` | `"#rrggbb"`, a colour word, or `null` | Buttons, the user's own words, highlights. `null` follows the critter's colour |
| `background` | `"#rrggbb"` or `null` | The page. `null` follows the theme |
| `surface` | `"#rrggbb"` or `null` | Cards and the prompt box |
| `text` | `"#rrggbb"` or `null` | Main text colour |
| `font` | `"rounded"`, `"mono"`, `"serif"`, `"pixel"`, or a path to a `.ttf` on the device | `mono` is the code-style font |
| `text_size` | `"small"`, `"medium"`, `"large"`, or a number of pixels from 10 to 40 | |
| `corners` | `"round"`, `"soft"`, `"square"` | |
| `fun` | `true` / `false` | `false` removes the critter, pixel headings and animations for a plain, professional look |
| `critter_size` | `"small"`, `"medium"`, `"large"` | |
| `speak` | `true` / `false` | Read replies aloud. Needs text-to-speech set up on this Hermes and a speaker on the device |
| `greeting` | text up to 80 characters, or `null` | The line on the home screen |
| `fresh_after_min` | minutes, `0` for never | After this long idle, the device starts a fresh conversation next time (keeps cost down) |
| `devices` | `{{"Device name": {{...any keys above...}}}}` | Overrides for one device, by its name |

Colour words understood: red, orange, yellow, green, cyan, blue, purple, pink, white, black, grey,
moss, mint, teal, sky, lilac, plum, rose, coral, amber, butter, stone.

## Pitfalls

- This is not Hermes's own appearance. Do not run `hermes skin` or change `display.*` or
  `desktop.*` in Hermes's config for a request about the Hermlet app.

- If you set `background` or `surface`, make sure `text` still contrasts with them, and say so if
  you had to change it too. Clearing all four colour keys to `null` returns to the built-in theme.
- "Only on the kitchen one" means a `devices` entry with that device's exact name; ask for the
  name if you do not know it.
- A `.ttf` path is a path on the device, not on this machine.
- Unknown keys are ignored by the app. Do not invent any.
- The critter's own name and colour live in `persona.json` next to this file. Rename or recolour
  it there (`"name"`, `"colour"`) only when the user asks about the critter itself.

## Verification

Read the file back and check it parses as JSON.
"""
