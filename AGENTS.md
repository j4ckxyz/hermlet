# AGENTS.md

How to build an app on Hermlet, and how to work on Hermlet itself. `README.md` is the shorter
overview for people.

## What this is

Hermlet connects a small Linux device (usually a Raspberry Pi) to its owner's self-hosted Hermes
Agent. There are three layers, and almost everything you will be asked to build belongs on top
of the first two, not inside them:

```
your app  ──►  hermlet.connect() / the local socket  ──►  hermlet run  ──►  Hermes
(any language)        docs/local-api.md                    (the service)
```

- **The service** (`hermlet run`) owns the one connection to Hermes, the microphone and the
  speaker. It is the only thing that holds credentials.
- **The local API** is JSON lines on a Unix socket. `hermlet.connect()` is a thin Python wrapper.
- **Apps** are separate programs. The native app (`app/`) is one of them and has no privileges
  your app lacks.

## Building an app

Start from `examples/`. The whole API is in `docs/local-api.md`; these are the patterns.

**Ask and get an answer**

```python
import hermlet

with hermlet.connect() as h:
    print(h.ask("What is on my calendar today?"))
```

**Tell the agent something happened, with a file**

```python
h.send("The mailbox sensor tripped. Here is the camera frame.", files=["/tmp/frame.jpg"])
```

**React to everything**

```python
for event in h.events():
    if event["type"] == "reply.delta":
        show(event["text"])          # cumulative: replace what you drew, do not append
    elif event["type"] == "turn.end":
        done()
```

**Offer the agent something to do on the device** (needs the plugin; see `docs/two-ways.md`)

```python
@h.action("lamp.set", "Turn the desk lamp on or off.",
          {"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]})
def lamp_set(on: bool):
    ...
    return {"on": on}

h.run_forever()
```

Rules that keep apps working:

1. **Never open your own connection to Hermes from an app.** Use the socket. One device is one
   connection; a second one fights the first.
2. **Never ask for, store or log the Hermes password** in an app. Sign-in belongs to
   `hermlet setup` and the bundled app's sign-in screen.
3. **Every turn reads** `user` → `turn.start` → (`status` | `tool` | `reply.delta`)* → `reply` →
   `turn.end`. Draw from the events, not from the return value of `send`, so your app stays in
   step with the other apps on the device.
4. **`reply.delta` is cumulative.** `reply` is the final text; show it even if you saw deltas.
5. **Check `h.state` before sending.** Anything but `ready` raises `HermletError` with a
   sentence you can show the person as it is.
6. **Describe actions for a model.** The `description` and JSON Schema are all the agent knows.
   Return something JSON-serialisable; raise to report a failure (the message reaches the agent).
7. **An action that changes the machine is the owner's decision.** Do not add shell, file-write
   or network-reconfiguring actions unless asked, and say plainly what they expose. See the
   warning at the top of `examples/shell_action.py`.
8. **Run it as a service** with a systemd user unit that has `After=hermlet.service`.

No Hermes to test against? `hermlet devserver` stands in for the plugin route:

```sh
hermlet devserver --port 8790 &
HERMLET_HOME=/tmp/dev hermlet setup ws://127.0.0.1:8790/hermlet --name Dev --no-service
HERMLET_HOME=/tmp/dev hermlet run &
HERMLET_HOME=/tmp/dev python your_app.py
```

It echoes messages and reacts to a few words so each part of an app can be exercised: `slow`
(status updates over a few seconds), `card`, `file`, `ask` (a yes/no prompt), `action <name>`.

## Layout

| Path | What it is |
|---|---|
| `src/hermlet/base.py` | `BaseDevice`: handlers, actions, the reconnect loop, the event vocabulary |
| `src/hermlet/dashboard.py` | `DashboardDevice`: the default route, Hermes's own web API. **The only file that speaks it** |
| `src/hermlet/device.py` | `HubDevice`: the plugin route |
| `src/hermlet/service.py` | `hermlet run`: the local socket, the chat log, the microphone |
| `src/hermlet/client.py` | `hermlet.connect()` |
| `src/hermlet/cli.py` | The `hermlet` command |
| `src/hermlet/audio.py` | Recording and playback through `arecord`/`aplay` and friends |
| `src/hermlet/persona.py` | The critter generator, and the line written to the agent's notes. **Byte-identical to `plugin/persona.py`** |
| `src/hermlet/look.py` | The shared look: its keys, cleaning, the agent's skill text, and the hidden note that opens each conversation. **Byte-identical to `plugin/look.py`** |
| `src/hermlet/net.py` | Finds a Tailscale name when the machine does not use Tailscale's DNS |
| `src/hermlet/devserver.py` | A stand-in for the plugin, for development |
| `plugin/` | The optional Hermes platform plugin. Self-contained: it is installed into Hermes on its own |
| `plugin/hub.py`, `store.py`, `protocol.py`, `persona.py`, `textfmt.py` | No Hermes imports; shared with the dev server |
| `plugin/adapter.py`, `tools.py`, `cli.py` | The Hermes-facing side |
| `app/` | The native app: C, LVGL, one binary |
| `tests/` | See below |
| `prompts/` | Setup prompts for the owner's Hermes and for an agent on the device |

## Tests

```sh
uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -q          # about 15 seconds; no network beyond 127.0.0.1
```

| File | Covers |
|---|---|
| `tests/test_dashboard.py` | The default route against `tests/fakes/dashboard.py`, a stand-in for the Hermes dashboard |
| `tests/test_device.py` | The plugin route against the real hub with the dev server's brain |
| `tests/test_service.py` | The local socket and `hermlet.connect()` |
| `tests/test_parity.py` | The two copies of `persona.py` and `protocol.py` agree |
| `tests/test_plugin_e2e.py` | The plugin inside a real `hermes gateway run`. Opt-in: `HERMLET_E2E=1 HERMES_AGENT_DIR=/path/to/hermes-agent` (a checkout with `uv sync` done) |

Unix socket paths are limited to about 100 characters, so tests that start a service use the
`short_tmp` fixture, not `tmp_path`.

A test that hangs is usually an event that never arrived. Run it with
`-o faulthandler_timeout=20` to see where it is waiting.

## The app

```sh
cmake -S app -B app/build -DCMAKE_BUILD_TYPE=Release && cmake --build app/build -j
app/build/hermlet-app                      # needs SDL2 (libsdl2-dev; brew install sdl2)
```

- `app/src/ui.c` is the whole interface. It rebuilds from a small model (`S`) whenever the theme
  or the critter changes; add state to `S`, never to widgets.
- The app talks only to the local socket (`app/src/ipc.c`). Do not add networking to it.
- The app starts the service (`hermlet run`) itself when nothing answers on the socket, and does
  first-run setup on screen (state `setup`: an address form, then sign-in, then naming the
  critter). `--no-spawn` stops it starting anything, which is what you want for pictures and
  tests against a service you started yourself.
- Check your work with a picture, without opening a window:

  ```sh
  SDL_VIDEODRIVER=dummy app/build/hermlet-app --no-spawn --size 800x480 \
      --screenshot /tmp/shot.bmp --after 1200 [--screen history|settings|attach|change-address]
  ```

  Look at it at 800x480 and at 320x240, in light and in dark (`"theme"` in `config.json`).
- Colours come from `pick_colours()`; the accent is the critter's body colour. Do not hard-code a
  colour in a widget.
- `-DHERMLET_BACKEND=offscreen` needs nothing but libc and draws into memory: use it for pictures
  and on machines with no screen. `-DHERMLET_BACKEND=fbdev` is the Linux framebuffer with no SDL;
  it compiles on a Pi 5 but has never been run.
- `apply_look()` decides every colour, face and size from the look. Add a look key in `look.py`
  (both copies), then in `take_look()` and `apply_look()`, then in Settings and the skill text.
- `HERMLET_THEME=dark|light` and `HERMLET_TOUCH=1|0` override one screen without touching the
  shared look; handy for pictures.

## Working on the routes

**Dashboard route.** Hermes's `/api/ws` is the TUI-gateway JSON-RPC its desktop app uses. The
contract is generated into the Hermes repository at
`apps/shared/src/gateway-contract.openrpc.json`; read the method's params and result there
before using one, and keep `tests/fakes/dashboard.py` in the same shape. Unknown param keys are
rejected. Server-to-client requests (`approval`, `clarify`) are JSON-RPC requests the client must
answer, and only reach a client that called `client.capabilities {"server_requests": true}`.

**Plugin route.** `plugin/adapter.py` is a Hermes `BasePlatformAdapter`; Hermes's
`gateway/platforms/ADDING_A_PLATFORM.md` is the reference. `plugin/hub.py` must not import
Hermes. If you change a message, change `docs/protocol.md` and both `protocol.py` files, and run
the opt-in end-to-end test.

## Things that will bite

- **The agent must be told where it is.** A request like "make the accent red" made on a device
  is ambiguous to Hermes, and without the hidden note that opens each conversation
  (`look.context`) it restyled *itself* in a live test. Keep that note short (it is sent with
  every new conversation), and if you change it, repeat the test in `docs/tested.md`.
- **Never test look or critter changes against someone's live Hermes without cleaning up.** They
  write to its home (`plugin-data/hermlet/`, `skills/hermlet/`, `memories/MEMORY.md`).

- **`persona.py` and `look.py` each exist twice** (`src/hermlet/` and `plugin/`). Edit one, copy
  it over the other; `tests/test_parity.py` fails otherwise.
- **The hub must never await the delegate on its receive loop.** The delegate may call back into
  the device (an action) and wait for an answer that arrives on that same loop. Spawn it.
- **Continuing another surface's conversation copies it.** Writing into a session that the
  messaging gateway (a different process) is using is not safe; both routes start a new
  `hermlet` session seeded with the history.
- **Uploads on the dashboard route are one WebSocket frame**, base64, so the cap is 10 MB.
- **Hermes rotates the dashboard access cookie.** `_Http` writes every `Set-Cookie` back to
  `auth.json`; a client that ignores them is signed out within 12 hours.
- **Python 3.9 is the floor** (Raspberry Pi OS Bullseye). No `match`, no `X | Y` outside
  annotations, no `tomllib`.

## Before you hand back work

1. `.venv/bin/python -m pytest -q` passes, and `python -m pyflakes src plugin tests examples` is quiet.
2. If you touched the plugin, the opt-in end-to-end test passes against a current Hermes.
3. If you touched the app, it builds without warnings and you looked at screenshots in both
   themes and both sizes.
4. If you touched `install.sh`, `bash -n install.sh` passes (and ShellCheck, if you have it).
5. Docs say what is true now: `docs/local-api.md` for ops and events, `docs/two-ways.md` for
   what each route can do and what has been tested. Do not mark something as working that you
   did not run.
