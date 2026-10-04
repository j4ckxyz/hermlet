"""The agent's critter: one procedurally drawn pixel companion per Hermes agent.

The persona is generated once on the Hermes host, kept in the plugin's data
directory and handed to every device in ``welcome``. All devices of one agent
therefore show the same critter, and a different agent gets a different one.

A persona is plain data a device can draw without any image decoder::

    {"version": 1, "seed": "9f2c...", "name": "Pib", "species": "cat", "colour": "#4fb3a6", "chosen": true,
     "palette": {"body": "#4fb3a6", "shade": "#3a8f84", "outline": "#1f3d39",
                 "accent": "#f2a541", "eye": "#1b1b1f", "cheek": "#f28ca0", "white": "#ffffff"},
     "legend": {".": null, "b": "body", "s": "shade", "o": "outline", "a": "accent",
                "e": "eye", "c": "cheek", "w": "white"},
     "sprite": {"w": 16, "h": 16, "frames": {"idle": ["................", ...], ...}}}

Each frame is ``h`` strings of ``w`` legend characters. ``chosen`` is true once
the owner has named and coloured the critter themselves; until then a device
offers to. No Hermes imports.
"""

from __future__ import annotations

import colorsys
import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any

VERSION = 1
SIZE = 16
HALF = SIZE // 2
FRAMES = ("idle", "blink", "talk", "think", "happy", "sleep")
LEGEND = {".": None, "b": "body", "s": "shade", "o": "outline", "a": "accent",
          "e": "eye", "c": "cheek", "w": "white"}
SPECIES = ("blob", "cat", "bunny", "bear", "sprout", "ghost", "bot", "chick")

# The colours an owner can pick from, in the order a device shows them.
COLOURS = (
    ("moss", "#9ccb5a"), ("mint", "#6fd3a8"), ("teal", "#4fb3a6"), ("sky", "#69b7e8"),
    ("blue", "#6f8ff0"), ("lilac", "#a98fe6"), ("plum", "#c77fd6"), ("rose", "#ec7fa8"),
    ("coral", "#f08c6c"), ("amber", "#f2b24a"), ("butter", "#ecd75f"), ("stone", "#a9a39b"),
)

_ONSETS = ("p", "b", "m", "n", "t", "k", "d", "l", "r", "s", "z", "w", "y", "h", "f", "g", "j")
_VOWELS = ("a", "i", "u", "e", "o", "oo", "ee")
_CODAS = ("b", "p", "n", "m", "k", "t", "x")


class _Rng:
    """A small deterministic stream, so a seed draws the same critter on every Python."""

    def __init__(self, seed: str):
        self._seed = seed.encode()
        self._n = 0
        self._buf = b""

    def _byte(self) -> int:
        if not self._buf:
            self._buf = hashlib.sha256(self._seed + self._n.to_bytes(4, "big")).digest()
            self._n += 1
        value, self._buf = self._buf[0], self._buf[1:]
        return value

    def below(self, n: int) -> int:
        return (self._byte() << 8 | self._byte()) % n

    def unit(self) -> float:
        return (self._byte() << 8 | self._byte()) / 65536.0

    def between(self, low: float, high: float) -> float:
        return low + (high - low) * self.unit()

    def pick(self, options):
        return options[self.below(len(options))]

    def chance(self, p: float) -> bool:
        return self.unit() < p


def _hex(h: float, s: float, l: float) -> str:
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360.0, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _name(rng: _Rng) -> str:
    """Short and soft: Pib, Momo, Tuki, Noop."""
    if rng.chance(0.4):
        return (rng.pick(_ONSETS) + rng.pick(_VOWELS) + rng.pick(_CODAS)).capitalize()
    first = rng.pick(_ONSETS) + rng.pick(_VOWELS[:5])
    second = first if rng.chance(0.25) else rng.pick(_ONSETS) + rng.pick(_VOWELS[:5])
    return (first + second).capitalize()


def suggest_name(seed: str | None = None) -> str:
    """A name the critter makes up for itself: a new one each time unless a seed is given."""
    return _name(_Rng("name:" + (seed or secrets.token_hex(8))))


def clean_name(name: str | None) -> str:
    """A name as it will be stored: printable, single-spaced, at most 24 characters."""
    text = " ".join("".join(c for c in str(name or "") if c.isprintable() and c not in "\u00a7<>`\\").split())
    return text[:24].strip()


def colour_hex(colour: str | None) -> str | None:
    """``"teal"`` or ``"#4fb3a6"`` as a hex colour; None when it is neither."""
    text = str(colour or "").strip().lower()
    for name, value in COLOURS:
        if text == name:
            return value
    if len(text) == 7 and text[0] == "#" and all(c in "0123456789abcdef" for c in text[1:]):
        return text
    return None


def colour_name(value: str | None) -> str:
    """The nearest named colour to a hex value, for saying it in words."""
    target = colour_hex(value)
    if not target:
        return ""
    rgb = [int(target[i:i + 2], 16) for i in (1, 3, 5)]
    return min(COLOURS, key=lambda c: sum((x - int(c[1][i:i + 2], 16)) ** 2 for x, i in zip(rgb, (1, 3, 5))))[0]


def _palette_for(body: str, rng: _Rng) -> dict[str, str]:
    """A whole palette built around a body colour the owner picked."""
    r, g, b = (int(body[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
    h, light, sat = colorsys.rgb_to_hls(r, g, b)
    return _palette_from(h * 360, sat, light, rng, body=body)


def _palette(rng: _Rng) -> dict[str, str]:
    hue = rng.between(0, 360)
    sat = rng.between(0.50, 0.72)
    light = rng.between(0.60, 0.70)
    return _palette_from(hue, sat, light, rng)


def _palette_from(hue: float, sat: float, light: float, rng: _Rng, body: str | None = None) -> dict[str, str]:
    accent_hue = hue + rng.pick((150, 180, 210, 35, -35, 120))
    palette = {
        "body": _hex(hue, sat, light),
        "shade": _hex(hue - 8, sat * 0.95, light - 0.13),
        "outline": _hex(hue - 12, sat * 0.6, 0.20),
        "accent": _hex(accent_hue, min(0.85, sat + 0.15), 0.64),
        "eye": _hex(hue, 0.25, 0.10),
        "cheek": _hex(350 if abs(((hue - 350) + 180) % 360 - 180) > 40 else 25, 0.80, 0.74),
        "white": "#ffffff",
    }
    if body:
        palette["body"] = body
        if sat < 0.15:  # a grey critter keeps a lively accent
            palette["accent"] = _hex(accent_hue, 0.7, 0.64)
    return palette


def _blank() -> list[list[str]]:
    return [["."] * SIZE for _ in range(SIZE)]


def _put(grid: list[list[str]], x: int, y: int, ch: str) -> None:
    """Set a pixel and its mirror image."""
    if 0 <= y < SIZE and 0 <= x < SIZE:
        grid[y][x] = ch
        grid[y][SIZE - 1 - x] = ch


def _body(rng: _Rng, species: str) -> tuple[list[list[str]], dict[str, int]]:
    """Fill the mirrored body mask and return where the face goes."""
    grid = _blank()
    top = {"bunny": 6, "cat": 5, "bear": 5, "sprout": 5, "bot": 5}.get(species, 4)
    bottom = 13
    half_w = rng.pick((5, 6, 6)) if species != "bot" else 6
    squareness = {"bot": 5.0, "ghost": 3.0}.get(species, rng.between(2.2, 3.2))
    cy = (top + bottom) / 2.0
    ry = (bottom - top + 1) / 2.0
    for y in range(top, bottom + 1):
        t = abs((y + 0.5 - (cy + 0.5)) / ry)
        w = half_w * (1.0 - min(1.0, t) ** squareness) ** (1.0 / squareness)
        if species == "ghost" and y > cy:
            w = half_w  # straight sides down to the hem
        for i in range(max(1, round(w))):
            _put(grid, HALF - 1 - i, y, "b")
    # Feet, hems and tails.
    if species == "ghost":
        for i in range(half_w):
            if i % 2 == 0:
                _put(grid, HALF - 1 - i, bottom + 1, "b")
    elif species != "blob" or rng.chance(0.5):
        foot = rng.pick((2, 3))
        _put(grid, HALF - foot, bottom + 1, "a" if species in ("chick", "bot") else "b")
        _put(grid, HALF - foot - 1, bottom + 1, "a" if species in ("chick", "bot") else "b")
    # Ears, antennae, leaves.
    if species == "cat":
        ex = HALF - half_w + 1
        for dx, dy in ((0, -1), (1, -1), (0, -2), (1, 0), (2, 0)):
            _put(grid, ex + dx, top + dy, "b")
        _put(grid, ex + 1, top - 1, "a")
    elif species == "bunny":
        ex = HALF - 3
        for dy in range(1, 6):
            _put(grid, ex, top - dy, "b")
            _put(grid, ex + 1, top - dy, "a" if 1 < dy < 5 else "b")
    elif species == "bear":
        ex = HALF - half_w + 1
        for dx, dy in ((0, -1), (1, -1), (0, 0), (1, -2), (2, -1)):
            _put(grid, ex + dx, top + dy, "b")
        _put(grid, ex + 1, top - 1, "a")
    elif species == "sprout":
        _put(grid, HALF - 1, top - 1, "a")
        _put(grid, HALF - 2, top - 2, "a")
        _put(grid, HALF - 3, top - 3, "a")
        _put(grid, HALF - 2, top - 3, "a")
    elif species == "bot":
        _put(grid, HALF - 1, top - 1, "s")
        _put(grid, HALF - 1, top - 2, "s")
        _put(grid, HALF - 1, top - 3, "a")
        _put(grid, HALF - half_w - 1, int(cy), "s")
    elif species == "chick":
        _put(grid, HALF - 1, top - 1, "a")
        _put(grid, HALF - 2, top - 2, "a")
        _put(grid, HALF - half_w - 1, int(cy) + 1, "b")
    elif species == "blob" and rng.chance(0.6):
        _put(grid, HALF - half_w - 1, int(cy) + 1, "b")  # little arms
    # A belly patch in the accent colour for some critters, kept clear of the face.
    eye_y = top + 3
    if species in ("bear", "chick") or (species in ("blob", "cat", "bunny") and rng.chance(0.45)):
        for y in range(eye_y + 4, bottom + 1):
            for i in range(3 if y < bottom else 2):
                _put(grid, HALF - 1 - i, y, "a")
    return grid, {"top": top, "bottom": bottom, "half_w": half_w, "eye_y": eye_y}


def _shade_and_outline(grid: list[list[str]]) -> list[list[str]]:
    solid = [[c != "." for c in row] for row in grid]
    out = [row[:] for row in grid]
    for y in range(SIZE):
        for x in range(SIZE):
            if grid[y][x] == "b":
                below = y + 1 >= SIZE or not solid[y + 1][x]
                below2 = y + 2 >= SIZE or not solid[y + 2][x]
                if below or (below2 and solid[y + 1][x] and grid[y + 1][x] == "b"):
                    out[y][x] = "s"
            elif not solid[y][x]:
                near = any(0 <= y + dy < SIZE and 0 <= x + dx < SIZE and solid[y + dy][x + dx]
                           for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
                if near:
                    out[y][x] = "o"
    return out


def _faces(rng: _Rng, base: list[list[str]], geom: dict[str, int], species: str) -> dict[str, list[str]]:
    eye_y = geom["eye_y"]
    eye_style = rng.pick(("dot", "tall", "shine", "shine"))
    eye_x = HALF - 4 if eye_style == "shine" else HALF - rng.pick((3, 3, 4))
    mouth_style = rng.pick(("smile", "dot", "cat", "none")) if species != "chick" else "beak"
    cheeks = rng.chance(0.7) and species != "bot"
    mouth_y = eye_y + 2

    def draw(eyes: str, mouth: str) -> list[str]:
        g = [row[:] for row in base]
        ey = eye_y - (1 if eyes == "up" else 0)
        if eyes in ("open", "up"):
            _put(g, eye_x, ey, "e")
            if eye_style in ("tall", "shine"):
                _put(g, eye_x, ey + 1, "e")
            if eye_style == "shine":
                # A 2x2 eye with the highlight top-left on both sides, so the gaze has a direction.
                _put(g, eye_x + 1, ey, "e")
                _put(g, eye_x + 1, ey + 1, "e")
                g[ey][eye_x] = "w"
                g[ey][SIZE - 2 - eye_x] = "w"
        elif eyes == "closed":
            row = eye_y + (1 if eye_style in ("tall", "shine") else 0)
            _put(g, eye_x, row, "e")
            _put(g, eye_x + 1, row, "e")
        elif eyes == "joy":
            row = eye_y + (1 if eye_style in ("tall", "shine") else 0)
            _put(g, eye_x, row, "e")
            _put(g, eye_x + 1, row - 1, "e")
            if eye_x + 2 < HALF - 1:
                _put(g, eye_x + 2, row, "e")
        if cheeks:
            _put(g, eye_x - 1 if eye_x - 1 >= HALF - geom["half_w"] + 1 else eye_x, mouth_y, "c")
        if mouth == "open":
            _put(g, HALF - 1, mouth_y, "e")
            _put(g, HALF - 1, mouth_y + 1, "c" if mouth_style != "beak" else "a")
        elif mouth == "rest":
            if mouth_style == "smile":
                _put(g, HALF - 1, mouth_y, "e")
            elif mouth_style == "dot":
                g[mouth_y][HALF - 1] = "e"
            elif mouth_style == "cat":
                _put(g, HALF - 2, mouth_y, "e")
                _put(g, HALF - 1, mouth_y + 1, "e")
            elif mouth_style == "beak":
                _put(g, HALF - 1, mouth_y, "a")
        elif mouth == "small":
            g[mouth_y][HALF - 1] = "e"
        return ["".join(row) for row in g]

    return {
        "idle": draw("open", "rest"),
        "blink": draw("closed", "rest"),
        "talk": draw("open", "open"),
        "think": draw("up", "small"),
        "happy": draw("joy", "open"),
        "sleep": draw("closed", "none"),
    }


def generate(seed: str | None = None, *, name: str | None = None, species: str | None = None,
             colour: str | None = None, chosen: bool = False) -> dict[str, Any]:
    """Draw a persona. The same seed always gives the same critter.

    ``colour`` (a name from ``COLOURS`` or a hex value) and ``name`` are the owner's
    choices; the shape and face still come from the seed, so recolouring or renaming
    never changes who the critter is.
    """
    seed = seed or secrets.token_hex(8)
    rng = _Rng(seed)
    drawn_name = _name(rng)
    drawn_species = rng.pick(SPECIES)
    palette = _palette(rng)
    picked = colour_hex(colour)
    if picked:
        palette = _palette_for(picked, _Rng(seed + ":palette"))
    kind = species if species in SPECIES else drawn_species
    body, geom = _body(rng, kind)
    frames = _faces(rng, _shade_and_outline(body), geom, kind)
    return {
        "version": VERSION,
        "seed": seed,
        "name": clean_name(name) or drawn_name,
        "species": kind,
        "colour": palette["body"],
        "chosen": bool(chosen),
        "palette": palette,
        "legend": dict(LEGEND),
        "sprite": {"w": SIZE, "h": SIZE, "frames": frames},
    }


def valid(persona: Any) -> bool:
    try:
        sprite = persona["sprite"]
        w, h = int(sprite["w"]), int(sprite["h"])
        if not (4 <= w <= 64 and 4 <= h <= 64) or "idle" not in sprite["frames"]:
            return False
        for rows in sprite["frames"].values():
            if len(rows) != h or any(len(r) != w for r in rows):
                return False
        return isinstance(persona["palette"], dict) and bool(str(persona["name"]).strip())
    except (KeyError, TypeError, ValueError):
        return False


MEMORY_MARK = "Hermlet:"
MEMORY_DELIMITER = "\n\u00a7\n"  # how Hermes separates entries in MEMORY.md


def memory_note(persona: dict[str, Any]) -> str:
    """One line for the agent's own notes, so it knows its critter by name on every surface."""
    name, shade = persona.get("name") or "Hermlet", colour_name(persona.get("colour"))
    look = f"a {shade} {persona.get('species')}" if shade else f"a {persona.get('species')}"
    return (f"{MEMORY_MARK} on the user's Hermlet devices (small screens that talk to me) I appear as a pixel "
            f"critter named {name}, {look}. \"{name}\" or \"my hermlet\" means that critter.")


def with_note(memory_text: str, note: str, limit: int) -> str | None:
    """``MEMORY.md`` with the hermlet note added or replaced; None when it would not fit."""
    entries = [e for e in memory_text.split(MEMORY_DELIMITER) if e.strip()] if memory_text.strip() else []
    kept = [e for e in entries if not e.lstrip().startswith(MEMORY_MARK)]
    updated = MEMORY_DELIMITER.join(kept + [note])
    return updated if len(updated) <= limit else None


class PersonaStore:
    """``persona.json`` in the plugin's data directory; created on first use."""

    FILENAME = "persona.json"

    def __init__(self, directory: Path | str):
        self._path = Path(directory) / self.FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, Any]:
        try:
            persona = json.loads(self._path.read_text(encoding="utf-8"))
            if valid(persona):
                return persona
        except (OSError, ValueError):
            pass
        return self.save(generate())

    def save(self, persona: dict[str, Any]) -> dict[str, Any]:
        if not valid(persona):
            raise ValueError("not a valid persona")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(persona, indent=1), encoding="utf-8")
        os.replace(tmp, self._path)
        return persona

    def mtime(self) -> float:
        try:
            return self._path.stat().st_mtime
        except OSError:
            return 0.0


def to_ansi(persona: dict[str, Any], frame: str = "idle") -> str:
    """The critter as terminal half-blocks (24-bit colour), two pixel rows per text row."""
    rows = persona["sprite"]["frames"].get(frame) or persona["sprite"]["frames"]["idle"]
    legend, palette = persona.get("legend") or LEGEND, persona["palette"]

    def rgb(ch: str):
        key = legend.get(ch)
        value = palette.get(key) if key else None
        return None if not value else tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))

    lines = []
    for y in range(0, len(rows), 2):
        line = ""
        lower = rows[y + 1] if y + 1 < len(rows) else "." * len(rows[y])
        for top, bot in zip(rows[y], lower):
            t, b = rgb(top), rgb(bot)
            if t is None and b is None:
                line += "\x1b[0m "
            elif t is None:
                line += "\x1b[0m\x1b[38;2;%d;%d;%dm▄" % b
            elif b is None:
                line += "\x1b[0m\x1b[38;2;%d;%d;%dm▀" % t
            else:
                line += "\x1b[38;2;%d;%d;%dm\x1b[48;2;%d;%d;%dm▀" % (t + b)
        lines.append(line + "\x1b[0m")
    return "\n".join(lines)


if __name__ == "__main__":  # python plugin/persona.py [seed ...]  -> preview critters
    import sys

    for s in sys.argv[1:] or [secrets.token_hex(4) for _ in range(4)]:
        p = generate(s)
        print(f"{p['name']} ({p['species']}, seed {s})")
        print(to_ansi(p))
