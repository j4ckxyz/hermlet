"""The ``hermlet`` command.

    hermlet setup        connect this machine to your Hermes
    hermlet run          keep it connected (the service apps talk to)
    hermlet chat         talk to your agent in the terminal
    hermlet send         send one message, optionally with files
    hermlet sessions     past conversations from every surface
    hermlet show ID      read one of them
    hermlet look         how the app looks: colours, font, size, plain or playful
    hermlet critter      your hermlet's name and colour
    hermlet status       what state this device is in
    hermlet doctor       check everything a hermlet needs
    hermlet service      install or remove the background service
    hermlet devserver    a stand-in for the Hermlet plugin, for building apps
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterator, List, Optional

from . import __version__, audio, config, mode_for, net
from .base import HermletError
from .client import Hermlet, connect
from .persona import COLOURS, colour_hex, to_ansi

UNIT = """[Unit]
Description=Hermlet: this machine's connection to your Hermes agent
After=network-online.target
Wants=network-online.target

[Service]
ExecStart={exe} run
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
"""


def say(text: str = "") -> None:
    print(text, flush=True)


def fail(text: str) -> "int":
    print(f"hermlet: {text}", file=sys.stderr)
    return 1


@contextlib.contextmanager
def link(start: bool = True) -> Iterator[Hermlet]:
    """The local service, started inside this process for the moment when it is not running."""
    try:
        client = connect()
    except HermletError:
        if not start:
            raise
        if not config.load().get("url"):
            raise HermletError("this machine is not set up yet; open the app, or run 'hermlet setup'") from None
        _serve_in_background()
        client = connect()
    try:
        yield client
    finally:
        client.close()


def _serve_in_background() -> None:
    from .service import Service

    ready = threading.Event()
    failure: List[BaseException] = []

    def target() -> None:
        async def main() -> None:
            try:
                service = Service()
                await service.start()
            except BaseException as exc:
                failure.append(exc)
                ready.set()
                return
            ready.set()
            try:
                await service.device.run()
            finally:
                await service.stop()
        asyncio.run(main())

    threading.Thread(target=target, name="hermlet-service", daemon=True).start()
    ready.wait(10)
    if failure:
        raise HermletError(str(failure[0]))


MEMORY_WORDS = {
    "saved": "Hermes knows too: its own notes now mention {name}.",
    "full": "Saved. Hermes's own notes are full, so it was not told; say \"my hermlet is called {name}\" to it yourself.",
    "off": "Saved. Hermes has its memory turned off, so it was not told.",
    "failed": "Saved, but Hermes could not be told. It will still show {name} on every device.",
}


def choose_critter(client: Hermlet, name: Optional[str], colour: Optional[str]) -> None:
    result = client.set_persona(name, colour)
    persona = result["persona"] or {}
    say()
    say(to_ansi(persona))
    say(f"\n  {persona.get('name')} the {persona.get('species')}")
    say("  " + MEMORY_WORDS.get(str(result.get("memory")), MEMORY_WORDS["failed"]).format(name=persona.get("name")))


def ask_about_critter(client: Hermlet) -> None:
    """First run on a Hermes nobody has named a critter for: ask, or let it choose for itself."""
    drawn = (client.status.get("persona") or {}).get("name") or "it"
    say("\nYour Hermes gets a little critter of its own: your hermlet. Every device shows the same one.")
    name = input(f"What should it be called? [Enter to let it pick: {drawn}] ").strip()
    while True:
        colour = input("Its colour? " + ", ".join(n for n, _ in COLOURS) + " [Enter to keep its own] ").strip()
        if not colour or colour_hex(colour):
            break
        say("  I do not know that colour.")
    choose_critter(client, name or None, colour or None)


def wait_for_state(client: Hermlet, wanted=("ready",), timeout: float = 25) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.refresh().get("state")
        if state in wanted or state in ("signin", "pairing", "setup"):
            return str(state)
        time.sleep(0.3)
    return client.state


def _ago(ts: float) -> str:
    seconds = max(0, time.time() - (ts or 0))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


# -- commands -------------------------------------------------------------------------


def cmd_setup(args: argparse.Namespace) -> int:
    cfg = config.load()
    saved = config.read_json(config.home() / "config.json", {}) or {}
    say("Hermlet setup\n")
    url = args.url or input(f"Address of your Hermes [{cfg.get('url') or 'e.g. https://hermes.your-tailnet.ts.net'}]: ").strip() or cfg.get("url")
    if not url:
        return fail("an address is needed; it is the one you open the Hermes dashboard at")
    name = args.name or input(f"A name for this device [{cfg['name']}]: ").strip() or cfg["name"]
    mode = "plugin" if args.plugin else mode_for(url, "")
    saved.update({"url": url, "name": name, "mode": mode})
    config.save(saved)
    if args.re_enroll:
        from .identity import Identity
        Identity.load().forget_enrollment()
    say(f"\nSaved. Connecting to {url} ({'the Hermlet plugin' if mode == 'plugin' else 'the Hermes dashboard'}) ...")
    try:
        with link() as client:
            client.request("setup", timeout=40, url=url, name=name, mode=mode)  # also redirects a service already running
            state = wait_for_state(client)
            if state == "signin":
                say("Your Hermes asks for the dashboard username and password.")
                say("They are used once to sign in; only the session is kept on this device.")
                for _ in range(3):
                    username = args.username or input("Username: ").strip()
                    password = os.environ.get("HERMLET_PASSWORD") or getpass.getpass("Password: ")
                    try:
                        client.request("signin", timeout=30, username=username, password=password)
                    except HermletError as exc:
                        say(f"  {exc}")
                        if args.username and os.environ.get("HERMLET_PASSWORD"):
                            return 1
                        continue
                    state = wait_for_state(client, timeout=30)
                    break
            if state == "pairing":
                pairing = client.refresh().get("pairing") or {}
                say(f"\nApprove this device on your Hermes. Code: {pairing.get('code', '(waiting)')}")
                say(f"  On the Hermes host run:  {pairing.get('command', 'hermes hermlet pair')}")
                say("Waiting for approval (Ctrl+C to stop; 'hermlet run' keeps waiting in the background) ...")
                state = wait_for_state(client, timeout=600)
            status = client.refresh()
            if state != "ready":
                return fail(f"not connected yet (state: {state}). 'hermlet doctor' can tell you why.")
            if args.critter_name or args.critter_colour:
                choose_critter(client, args.critter_name, args.critter_colour)
            elif not status.get("persona_chosen") and sys.stdin.isatty():
                ask_about_critter(client)
            elif status.get("persona"):
                say()
                say(to_ansi(status["persona"]))
                say(f"\n  {status['persona'].get('name')} is your hermlet. Every device of this Hermes shows the same one."
                    + ("" if status.get("persona_chosen") else "\n  Name and colour it any time with: hermlet critter --name ... --colour ..."))
            say(f"\nConnected as '{name}'. Try:  hermlet chat")
    except HermletError as exc:
        return fail(str(exc))
    if not args.no_service and shutil.which("systemctl") and sys.stdin.isatty():
        if input("\nKeep this device connected in the background (recommended)? [Y/n] ").strip().lower() in ("", "y", "yes"):
            return cmd_service(argparse.Namespace(action="install"))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .service import Service

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("websockets").setLevel(logging.WARNING)
    if not config.load().get("url"):
        say("No Hermes address yet. Open the app (hermlet-app) or run 'hermlet setup' to give it one; waiting.")
    try:
        asyncio.run(Service().run())
    except KeyboardInterrupt:
        pass
    except HermletError as exc:
        return fail(str(exc))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    try:
        with link(start=False) as client:
            status = client.refresh()
    except HermletError:
        cfg = config.load()
        say(f"not running here. Hermes address: {cfg.get('url') or '(not set up)'}")
        say("Start it with 'hermlet run', or 'hermlet service install' to keep it running.")
        return 3
    if args.json:
        say(json.dumps(status, indent=2))
        return 0
    persona = status.get("persona") or {}
    say(f"device   {status['device']['name']}  ({status.get('mode')})")
    say(f"hermes   {status.get('url')}")
    say(f"state    {status.get('state')}" + (f"  (busy: {status.get('status') or 'working'})" if status.get("busy") else ""))
    if status.get("pairing"):
        say(f"pairing  code {status['pairing'].get('code')}: {status['pairing'].get('command')}")
    say(f"critter  {persona.get('name', '?')} the {persona.get('species', '?')}")
    say(f"chat     {len(status.get('chat') or [])} messages" + (f" in '{status['session_title']}'" if status.get("session_title") else ""))
    say(f"voice    microphone {'ready' if (status.get('mic') or {}).get('available') else 'not found'}, "
        f"spoken replies {'on' if status.get('speak') else 'off'}")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    text = " ".join(args.text) if args.text else ("" if sys.stdin.isatty() else sys.stdin.read().strip())
    if not text and not args.attach:
        return fail("nothing to send")
    try:
        with link() as client:
            if wait_for_state(client) != "ready":
                return fail(f"not connected (state: {client.state}); see 'hermlet status'")
            if args.no_wait:
                client.send(text, args.attach)
            else:
                say(client.ask(text, args.attach, timeout=args.timeout))
    except HermletError as exc:
        return fail(str(exc))
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    try:
        with link() as client:
            if wait_for_state(client) != "ready":
                return fail(f"not connected (state: {client.state})")
            rows = client.sessions(limit=args.limit, source=args.source, query=args.query)
    except HermletError as exc:
        return fail(str(exc))
    if args.json:
        say(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        mark = "*" if row.get("current") else " "
        say(f"{mark} {row['id']:<28} {row['source']:<9} {_ago(row.get('last_active') or 0):<9} {row['title'][:60]}")
    if not rows:
        say("no conversations found")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    try:
        with link() as client:
            if wait_for_state(client) != "ready":
                return fail(f"not connected (state: {client.state})")
            messages = client.session_messages(args.id, limit=args.limit)
    except HermletError as exc:
        return fail(str(exc))
    for m in messages:
        say(f"\033[1m{'you' if m['role'] == 'user' else 'hermes'}\033[0m  {m['content']}\n")
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    try:
        with link() as client:
            if wait_for_state(client) != "ready":
                return fail(f"not connected (state: {client.state}); see 'hermlet status'")
            status = client.refresh()
            persona = status.get("persona") or {}
            if persona:
                say(to_ansi(persona))
            say(f"\n{persona.get('name', 'Hermes')} is listening. /new  /attach FILE  /sessions  /resume ID  /quit\n")
            for m in (status.get("chat") or [])[-6:]:
                say(f"\033[2m{'you' if m['role'] == 'user' else persona.get('name', 'hermes')}: {m['text'][:300]}\033[0m")
            pending: List[str] = []
            while True:
                try:
                    line = input("\033[1myou\033[0m  ").strip()
                except (EOFError, KeyboardInterrupt):
                    say()
                    return 0
                if not line:
                    continue
                if line in ("/quit", "/exit", "/q"):
                    return 0
                if line == "/new":
                    client.new_session()
                    say("  (a fresh conversation)")
                    continue
                if line.startswith("/attach"):
                    path = os.path.expanduser(line[7:].strip())
                    if os.path.isfile(path):
                        pending.append(path)
                        say(f"  (attached {os.path.basename(path)}; it goes with your next message)")
                    else:
                        say(f"  no such file: {path}")
                    continue
                if line == "/sessions":
                    for row in client.sessions(limit=12):
                        say(f"  {row['id']:<28} {row['source']:<9} {row['title'][:50]}")
                    continue
                if line.startswith("/resume"):
                    try:
                        session = client.resume(line[7:].strip())
                        say(f"  (continuing '{session.get('title')}')")
                    except HermletError as exc:
                        say(f"  {exc}")
                    continue
                try:
                    _converse(client, line, pending, persona.get("name", "hermes"))
                except HermletError as exc:
                    say(f"  {exc}")
                pending = []
    except HermletError as exc:
        return fail(str(exc))


def _converse(client: Hermlet, text: str, files: List[str], who: str) -> None:
    """One turn in the terminal: the reply appears as it is written."""
    shown = 0
    client.send(text, files)
    sys.stdout.write(f"\033[1m{who}\033[0m  ")
    sys.stdout.flush()
    for msg in client.events(timeout=600):
        kind = msg.get("type")
        if kind == "reply.delta":
            body = str(msg.get("text") or "")
            sys.stdout.write(body[shown:])
            sys.stdout.flush()
            shown = len(body)
        elif kind == "reply":
            body = str(msg.get("text") or "")
            sys.stdout.write((body[shown:] if body[:shown] and shown <= len(body) else ("\n" if shown else "") + body) + "\n")
            sys.stdout.flush()
            shown = 0
        elif kind == "status" and msg.get("text") and not shown:
            sys.stdout.write(f"\033[2m({msg['text']})\033[0m ")
            sys.stdout.flush()
        elif kind == "prompt":
            choices = msg.get("choices") or []
            answer = input(f"\n  {msg.get('title')}: {msg.get('text')}\n  {' / '.join(choices) if choices else 'your answer'} > ").strip()
            client.answer(str(msg["id"]), answer)
        elif kind == "notice":
            say(f"\n  \033[2m{msg.get('text')}\033[0m")
        elif kind == "file":
            say(f"\n  (saved {msg.get('path')})")
        elif kind == "turn.end":
            say()
            return


def cmd_critter(args: argparse.Namespace) -> int:
    if args.colour and not colour_hex(args.colour):
        return fail("not a colour I know; use one of: " + ", ".join(n for n, _ in COLOURS) + ", or a value like #4fb3a6")
    if args.name or args.colour or args.surprise:
        try:
            with link() as client:
                if wait_for_state(client) != "ready":
                    return fail(f"not connected (state: {client.state}); see 'hermlet status'")
                choose_critter(client, client.suggest_name() if args.surprise and not args.name else args.name, args.colour)
        except HermletError as exc:
            return fail(str(exc))
        return 0
    persona = config.read_json(config.home() / "persona.json")
    if not persona:
        return fail("no critter yet; connect once with 'hermlet setup'")
    say(to_ansi(persona, args.frame))
    say(f"\n  {persona.get('name')} the {persona.get('species')}")
    return 0


def cmd_look(args: argparse.Namespace) -> int:
    changes = {}
    for item in args.changes:
        key, sep, value = item.partition("=")
        if not sep:
            return fail(f"write changes as key=value, for example accent=red (got '{item}')")
        low = value.strip().lower()
        changes[key.strip()] = (True if low in ("true", "yes", "on") else False if low in ("false", "no", "off")
                                else None if low in ("null", "none", "default", "") else int(low) if low.isdigit() else value.strip())
    try:
        with link() as client:
            if wait_for_state(client) != "ready":
                return fail(f"not connected (state: {client.state}); see 'hermlet status'")
            look = client.set_look(this_device_only=args.this_device, **changes) if changes else client.refresh().get("look") or {}
    except HermletError as exc:
        return fail(str(exc))
    for key in sorted(look):
        say(f"{key:<13} {'(default)' if look[key] is None else look[key]}")
    if not changes:
        say("\nChange with: hermlet look accent=red font=mono text_size=large fun=false"
            "\nOr just ask your agent on the device: \"make the accent red\".")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    import urllib.request

    cfg = config.load()
    problems = 0

    def check(ok: Optional[bool], label: str, hint: str = "") -> None:
        nonlocal problems
        mark = "ok  " if ok else ("    " if ok is None else "FAIL")
        problems += ok is False
        say(f"[{mark}] {label}" + (f"\n         {hint}" if hint and not ok else ""))

    net.install()
    say(f"hermlet {__version__}, Python {sys.version.split()[0]}, home {config.home()}\n")
    url = cfg.get("url") or ""
    check(bool(url), f"Hermes address: {url or '(none)'}", "run 'hermlet setup'")
    if url:
        mode = mode_for(url, cfg.get("mode") or "")
        try:
            if mode == "dashboard":
                from .dashboard import normalize_base
                with urllib.request.urlopen(normalize_base(url) + "/api/status", timeout=8) as r:
                    status = json.loads(r.read())
                check(True, f"Hermes {status.get('version')} answers; sign-in {'required' if status.get('auth_required') else 'not required'}")
                for name, address in net.used.items():
                    check(None, f"{name} was found through Tailscale ({address}); this machine does not use Tailscale's DNS, which is fine")
                check(status.get("gateway_running") or None, "Hermes messaging gateway running (only needed for Discord, Telegram and friends)")
            else:
                from . import protocol
                http_url = protocol.normalize_url(url).replace("ws://", "http://").replace("wss://", "https://").rsplit("/", 1)[0] + "/health"
                with urllib.request.urlopen(http_url, timeout=8) as r:
                    check(r.status == 200, "the Hermlet plugin answers on your Hermes")
        except Exception as exc:
            check(False, f"reach Hermes: {exc}",
                  "is the Hermes dashboard running, and is this machine on the same Tailscale network? Try 'tailscale status'.")
    tailscale = shutil.which("tailscale")
    check(bool(tailscale) or None, "Tailscale installed" if tailscale else "Tailscale not found (fine if Hermes is on your local network)",
          "install it from https://tailscale.com/download so this device can reach Hermes from anywhere")
    try:
        with link(start=False) as client:
            status = client.refresh()
        check(status.get("state") == "ready", f"service running, state: {status.get('state')}",
              {"signin": "run 'hermlet setup' to sign in", "pairing": "approve the code shown by 'hermlet status' on your Hermes"}.get(
                  str(status.get("state")), "see 'journalctl --user -u hermlet -e'"))
    except HermletError:
        check(None, "service not running (start it with 'hermlet run' or 'hermlet service install')")
    check(audio.can_record() or None, "microphone tool (arecord)" if audio.can_record() else "no microphone tool; typing still works",
          "sudo apt install alsa-utils")
    check(audio.can_play() or None, "speaker tool (aplay)" if audio.can_play() else "no speaker tool; replies are shown, not spoken")
    say("\nall good" if not problems else f"\n{problems} problem(s) to fix")
    return 1 if problems else 0


def cmd_service(args: argparse.Namespace) -> int:
    unit = Path(os.path.expanduser("~/.config/systemd/user/hermlet.service"))
    if not shutil.which("systemctl"):
        return fail("systemd is not available here; run 'hermlet run' yourself, for example from tmux")

    def systemctl(*parts: str) -> int:
        return subprocess.call(["systemctl", "--user", *parts])

    if args.action == "install":
        exe = shutil.which("hermlet") or f"{sys.executable} -m hermlet"
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(UNIT.format(exe=exe), encoding="utf-8")
        systemctl("daemon-reload")
        code = systemctl("enable", "--now", "hermlet.service")
        # Without lingering, a user service stops at logout and does not start at boot.
        subprocess.call(["loginctl", "enable-linger", getpass.getuser()], stderr=subprocess.DEVNULL)
        say("hermlet is now running in the background and starts with this machine." if code == 0
            else "could not start the service; see 'systemctl --user status hermlet'")
        return code
    if args.action == "uninstall":
        systemctl("disable", "--now", "hermlet.service")
        with contextlib.suppress(OSError):
            unit.unlink()
        systemctl("daemon-reload")
        say("the background service is removed; your settings are kept")
        return 0
    return systemctl("status", "hermlet.service", "--no-pager")


def cmd_signout(args: argparse.Namespace) -> int:
    with contextlib.suppress(HermletError):
        with link(start=False) as client:
            client.request("signout")
    with contextlib.suppress(OSError):
        (config.home() / "auth.json").unlink()
    say("signed out; run 'hermlet setup' to sign in again")
    return 0


def cmd_devserver(args: argparse.Namespace) -> int:
    from . import devserver

    async def main() -> None:
        hub, brain = await devserver.serve(args.host, args.port, require_pairing=args.pairing, seed=args.seed)
        say(f"dev server (stands in for the Hermlet plugin) at ws://{args.host}:{hub.bound_port}/hermlet")
        say("try:  hermlet setup ws://%s:%d/hermlet   then words like: card, file, ask, slow, action device.health" % (args.host, hub.bound_port))
        if args.pairing:
            say("pairing is on: type a device's code here and press Enter to approve it")
            loop = asyncio.get_running_loop()
            while True:
                code = (await loop.run_in_executor(None, sys.stdin.readline)).strip().upper()
                if code:
                    say("approved" if await brain.approve(code) else "no device is showing that code")
        await asyncio.Event().wait()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="hermlet", description="Put your own Hermes agent on this machine.")
    parser.add_argument("--version", action="version", version=f"hermlet {__version__}")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("setup", help="connect this machine to your Hermes")
    p.add_argument("url", nargs="?", help="where you open your Hermes dashboard, e.g. https://hermes.your-tailnet.ts.net")
    p.add_argument("--name", help="a name for this device")
    p.add_argument("--username", help="dashboard username (the password is asked for, or read from HERMLET_PASSWORD)")
    p.add_argument("--plugin", action="store_true", help="connect through the Hermlet plugin instead of the dashboard")
    p.add_argument("--re-enroll", action="store_true", help="plugin mode: introduce this device to a Hermes that forgot it")
    p.add_argument("--no-service", action="store_true", help="do not offer to install the background service")
    p.add_argument("--critter-name", help="name your hermlet during setup instead of being asked")
    p.add_argument("--critter-colour", "--critter-color", help="colour your hermlet during setup (see 'hermlet critter --help')")
    p.set_defaults(fn=cmd_setup)

    p = sub.add_parser("run", help="keep this machine connected (the service apps talk to)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("status", help="what state this device is in")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("send", help="send one message and print the answer")
    p.add_argument("text", nargs="*")
    p.add_argument("-a", "--attach", action="append", default=[], metavar="FILE", help="a file or image to send along (repeatable)")
    p.add_argument("--no-wait", action="store_true", help="do not wait for the answer")
    p.add_argument("--timeout", type=float, default=600)
    p.set_defaults(fn=cmd_send)

    p = sub.add_parser("chat", help="talk to your agent in the terminal")
    p.set_defaults(fn=cmd_chat)

    p = sub.add_parser("sessions", help="past conversations from every surface")
    p.add_argument("--source", help="only one surface: discord, hermlet, cli, telegram, ...")
    p.add_argument("--query", "-q", help="search the conversations' text")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_sessions)

    p = sub.add_parser("show", help="read one conversation")
    p.add_argument("id")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("critter", help="show your hermlet, or give it a name and a colour")
    p.add_argument("--name", help="what to call it")
    p.add_argument("--colour", "--color", help="moss, mint, teal, sky, blue, lilac, plum, rose, coral, amber, butter, stone, or #rrggbb")
    p.add_argument("--surprise", action="store_true", help="let it make up its own name")
    p.add_argument("--frame", default="idle", choices=["idle", "blink", "talk", "think", "happy", "sleep"])
    p.set_defaults(fn=cmd_critter)

    p = sub.add_parser("look", help="show or change how the app looks, for every device of this Hermes")
    p.add_argument("changes", nargs="*", metavar="key=value",
                   help="theme=dark accent=red font=mono text_size=large corners=soft fun=false critter_size=small speak=true")
    p.add_argument("--this-device", action="store_true", help="change it for this device only")
    p.set_defaults(fn=cmd_look)

    p = sub.add_parser("doctor", help="check everything a hermlet needs")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("service", help="install or remove the background service")
    p.add_argument("action", choices=["install", "uninstall", "status"])
    p.set_defaults(fn=cmd_service)

    p = sub.add_parser("signout", help="forget the signed-in session on this device")
    p.set_defaults(fn=cmd_signout)

    p = sub.add_parser("devserver", help="a stand-in for the Hermlet plugin, for building apps with no Hermes")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--pairing", action="store_true", help="make devices show a pairing code first")
    p.add_argument("--seed", help="draw a particular critter")
    p.set_defaults(fn=cmd_devserver)

    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help()
        return 0
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
