"""``hermes hermlet ...``: look after hermlets from the Hermes host."""

from __future__ import annotations

import datetime as _dt
import sys
import time

PAIR_TIMEOUT_S = 300


def _data_dir():
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir("hermlet")


def _store():
    from .store import DeviceStore

    return DeviceStore(_data_dir())


def _extra() -> dict:
    try:
        from hermes_cli.config import load_config

        cfg = ((load_config() or {}).get("platforms") or {}).get("hermlet") or {}
        return {"enabled": bool(cfg.get("enabled")), **(cfg.get("extra") or {})}
    except Exception:
        return {}


def setup_argparse(parser) -> None:
    sub = parser.add_subparsers(dest="hermlet_command")
    sub.add_parser("devices", help="devices this Hermes knows, and when each was last seen")
    sub.add_parser("info", help="where devices should connect")
    pair = sub.add_parser("pair", help="approve a device that is showing a pairing code")
    pair.add_argument("code", nargs="?", help="the code on the device (omit to approve the one waiting)")
    pair.add_argument("--yes", action="store_true", help="do not ask before approving")
    forget = sub.add_parser("forget", help="forget a device so it can enroll again after a reset")
    forget.add_argument("device_id")
    persona = sub.add_parser("persona", help="show or change this agent's critter")
    persona.add_argument("--reroll", action="store_true", help="draw a new critter")
    persona.add_argument("--seed", help="draw the critter for a particular seed")
    persona.add_argument("--name", help="rename the critter")
    persona.add_argument("--colour", "--color", help="a colour name (moss, teal, rose, ...) or a hex value like #4fb3a6")
    persona.add_argument("--species", help="blob, cat, bunny, bear, sprout, ghost, bot or chick")


def _when(ts) -> str:
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "never"


def _devices() -> int:
    devices = _store().devices()
    if not devices:
        print("No hermlets have connected yet. On a device run: hermlet setup --plugin ws://<this host>:8790/hermlet")
        return 0
    for device_id, rec in sorted(devices.items(), key=lambda kv: -(kv[1].get("last_seen") or 0)):
        print(f"{device_id}  {rec.get('name') or '(unnamed)':<24} {rec.get('kind') or '':<16} last seen {_when(rec.get('last_seen'))}")
    return 0


def _info() -> int:
    extra = _extra()
    host, port, path = extra.get("host") or "0.0.0.0", extra.get("port") or 8790, extra.get("path") or "/hermlet"
    print(f"Hermlet platform: {'enabled' if extra.get('enabled') else 'NOT enabled (set platforms.hermlet.enabled: true)'}")
    print(f"Listening on {host}:{port}{path}")
    print(f"On a device:  hermlet setup --plugin ws://<this machine's Tailscale name>:{port}{path}")
    return 0


def _pair(code: str | None, assume_yes: bool) -> int:
    from gateway.pairing import PairingStore

    store, pairing = _store(), PairingStore()
    deadline = time.time() + PAIR_TIMEOUT_S
    announced = False
    while True:
        store.reload()
        waiting = store.pending_pairings()
        if code:
            waiting = {k: v for k, v in waiting.items() if v.get("code") == code.upper()}
        if waiting:
            break
        if time.time() > deadline:
            print("No device asked to pair. Start one with 'hermlet setup --plugin ...' and try again.")
            return 1
        if not announced:
            print("Waiting for a device to show a pairing code ...")
            announced = True
        time.sleep(1.5)
    if len(waiting) > 1:
        print("Several devices are waiting; name one:")
        for device_id, rec in waiting.items():
            print(f"  hermes hermlet pair {rec['code']}    # {rec.get('name') or device_id}")
        return 1
    device_id, rec = next(iter(waiting.items()))
    label = rec.get("name") or device_id
    if not assume_yes and sys.stdin.isatty():
        if input(f"Approve '{label}' ({device_id}, code {rec['code']})? [Y/n] ").strip().lower() not in ("", "y", "yes"):
            print("Not approved.")
            return 1
    approved = pairing.approve_code("hermlet", rec["code"])
    if not approved:
        print("That code is no longer valid; restart the device's setup to get a new one.")
        return 1
    print(f"Approved '{label}'. It is connected.")
    return 0


def _persona(args) -> int:
    from .persona import COLOURS, PersonaStore, colour_hex, generate, to_ansi

    personas = PersonaStore(_data_dir())
    persona = personas.load()
    if args.colour and not colour_hex(args.colour):
        print("Not a colour I know. Use one of: " + ", ".join(name for name, _ in COLOURS) + ", or a hex value like #4fb3a6")
        return 2
    if args.reroll or args.seed or args.species or args.name or args.colour:
        fresh = args.reroll or args.seed
        persona = generate(args.seed if args.seed else (None if args.reroll else persona.get("seed")),
                           name=args.name or (None if fresh else persona.get("name")),
                           species=args.species or (None if fresh else persona.get("species")),
                           colour=args.colour or (None if fresh else persona.get("colour")),
                           chosen=bool(args.name or args.colour or persona.get("chosen")))
        personas.save(persona)
    print(to_ansi(persona))
    print(f"\n{persona['name']} the {persona['species']} (seed {persona['seed']})")
    print("Every hermlet of this Hermes shows this critter; connected devices update within a few seconds.")
    return 0


def handle(args) -> int:
    command = getattr(args, "hermlet_command", None)
    if command == "devices":
        return _devices()
    if command == "info":
        return _info()
    if command == "pair":
        return _pair(args.code, args.yes)
    if command == "forget":
        removed = _store().forget(args.device_id)
        print("Forgotten; the device can enroll again." if removed else "No such device.")
        return 0 if removed else 1
    if command == "persona":
        return _persona(args)
    print("usage: hermes hermlet {devices,info,pair,forget,persona}")
    return 2
