# The local API

While the service (`hermlet run`) is up, every program on the machine can talk to Hermes through one Unix
socket. This is what the app, the `hermlet` command and `hermlet.connect()` all use, and it is
the interface to build against from any language.

- **Where:** `~/.config/hermlet/hermlet.sock` (`$HERMLET_HOME/hermlet.sock` when that is set).
  Mode 0600: only the account running the service can open it.
- **Framing:** UTF-8 JSON objects, one per line, in both directions.
- **No credentials:** the service holds the connection to Hermes. A client never sees a password
  or a token.
- **Any number of clients.** Every client sees every event, so two apps stay in step: what one
  sends, the other shows.

```sh
bash examples/raw_socket.sh "hello"      # a complete client in 15 lines
```

## Connecting

The service speaks first, with a snapshot:

```json
{"type": "hello", "version": 1, "mode": "dashboard",
 "device": {"id": "hermlet:Kitchen Pi", "name": "Kitchen Pi", "kind": "linux-aarch64"},
 "url": "https://hermes.tail1234.ts.net", "state": "ready", "error": "", "agent": "Hermes",
 "persona": {"name": "Juso", "species": "bear", "palette": {...}, "sprite": {...}},
 "persona_chosen": false, "swatches": [{"name": "moss", "hex": "#9ccb5a"}, ...],
 "look": {"theme": "auto", "accent": null, "font": "rounded", "text_size": "medium", "fun": true, ...},
 "pairing": null, "busy": false, "status": "", "prompt": null, "session_title": "Dinner ideas",
 "chat": [{"role": "user", "text": "what should I cook?", "ts": 1791128000.0},
          {"role": "assistant", "text": "How about lemon pasta?", "ts": 1791128003.1}],
 "mic": {"available": true, "recording": false}, "speaker": true, "speak": false, "theme": "auto",
 "inbox": "/home/pi/Hermlet"}
```

`chat` is the conversation the device is in (up to 200 messages), so an app that starts late
still shows it. `persona_chosen` is false until the owner has named the critter: an app should
offer to. `look` is how the app should appear (see [The look](#the-look)). `state` is one of:

| State | Meaning |
|---|---|
| `setup` | Nobody has said where Hermes is yet (send a `setup` op). `url` is empty and `mode` is `setup`. |
| `offline` | Not connected; the service keeps retrying. `error` (here and on `link` events) says why. |
| `connecting` | Reaching Hermes. |
| `signin` | Hermes wants a username and password (send a `signin` op). |
| `pairing` | Plugin mode: waiting for the owner to approve the code in `pairing`. |
| `ready` | Connected. Messages can be sent. |

The service starts with nothing configured (state `setup`), so an app can do the whole first
run itself: send `setup`, then `signin` if the state becomes `signin`, then `persona.set`. The
bundled app starts the service too when nothing is listening on the socket (`app/src/ipc.c`
shows how: it looks for the `hermlet` command beside itself, in `~/.local/bin`, then on `PATH`,
and runs `hermlet run` detached).

## Ops (client to service)

Every op is `{"op": "<name>", ...}`. Add `"req": "<anything>"` and the service answers that
client with `{"type": "result", "req": "<same>", "op": "<name>", "ok": true, ...}` or
`{"ok": false, "error": "a sentence a person can read"}`. Without `req` a success is silent;
a failure is still reported.

| Op | Fields | Result | What it does |
|---|---|---|---|
| `send` | `text`, `files` (absolute paths) | `id` | Send a message, with files and pictures alongside |
| `cancel` | | | Stop what Hermes is doing |
| `new` | | | Start a fresh conversation |
| `answer` | `id`, `answer` | | Answer a `prompt` (one of its choices, or free text) |
| `talk.start` | | | Start listening on the microphone |
| `talk.stop` | | | Stop listening and send what was said |
| `talk.cancel` | | | Stop listening and throw it away |
| `sessions.list` | `limit`, `offset`, `source`, `query` | `sessions` | Past conversations from every surface |
| `sessions.get` | `session_id`, `limit` | `session`, `messages` | One conversation's messages, oldest first |
| `sessions.resume` | `session_id` | `session` | Carry that conversation on here |
| `actions` | `actions` | | Offer actions the agent can call (plugin mode) |
| `action.result` | `id`, `ok`, `result` or `error` | | The answer to an `action` event |
| `event` | `name`, `data`, `notify` | | Report that something happened; with `notify` the agent is told |
| `report` | `sensors` | | Share readings the agent can look up |
| `status` | | `status` | The `hello` snapshot again |
| `look.set` | `changes` (an object of look keys), `this_device_only` | `look` | Change the look for every device of this Hermes, or just this one |
| `look.teach` | | `taught` | Write the skill that lets the agent restyle the app when asked |
| `persona.set` | `name`, `colour` (a swatch name or `#rrggbb`) | `persona`, `memory`, `taught` | Name and colour the critter. `memory` says whether the agent's notes now mention it: `saved`, `full`, `off`, `failed` |
| `persona.preview` | `name`, `colour` | `persona` | The critter as it would look. Nothing is saved |
| `persona.suggest` | | `name` | A name the critter makes up for itself |
| `set` | `theme` (`auto`, `light`, `dark`), `speak` (bool) | | The older, shorter form of `look.set` |
| `setup` | `url`, `name` (this device's, optional), `mode` (optional) | | Say where Hermes is, the first time or to move to another. A bare name such as `hermes.tail1234.ts.net` is enough: `https://` and then `http://`...`:9119` are tried. Every client then gets a fresh `hello` |
| `signin` | `username`, `password` | | Sign in to the Hermes dashboard |
| `signout` | | | Forget the signed-in session |

A session row is `{"id", "source", "title", "preview", "started_at", "last_active",
"message_count", "current"}`. `source` is where the conversation happened: `hermlet`, `discord`,
`telegram`, `cli`, `tui`, `desktop` and so on. Automation (`cron`, `subagent`) is left out unless
you ask for it with `source`.

`sessions.resume` on one of the device's own conversations reopens it. On a conversation from
another surface it starts a new `hermlet` conversation that begins with that history, so the
original stays exactly as it was in Discord (or wherever it lives).

## Events (service to client)

| Event | Fields | Meaning |
|---|---|---|
| `link` | `state`, `error` | The connection state changed |
| `user` | `id`, `text`, `files`, `ts` | Someone on this device sent a message |
| `uploading` | `files` | Files are on their way to Hermes |
| `turn.start` | | Hermes began working |
| `status` | `text` | What it is doing ("Web search: weather lisbon"); empty clears it |
| `tool` | `name`, `state` (`start`, `done`) | A tool call began or finished (dashboard mode) |
| `reply.delta` | `text` | The reply so far. **Cumulative**: replace, do not append |
| `reply` | `text`, `interim` | A finished reply. `interim` marks commentary before the answer |
| `turn.end` | `outcome` (`success`, `failure`, `cancelled`) | The turn is over |
| `transcript` | `text` | What speech-to-text heard |
| `mic` | `state` (`recording`, `idle`), `level` (0 to 1) | Microphone state, for a meter |
| `notice` | `text`, `ttl_s` | A short message to show for a while |
| `card` | `title`, `body`, `ttl_s` | A card the agent put on the screen (plugin mode) |
| `prompt` | `id`, `title`, `text`, `choices`, `free_text` | A question for the person. Answer with the `answer` op |
| `prompt.close` | `id` | That question was withdrawn or answered elsewhere |
| `file` | `path`, `name`, `mime`, `caption` | A file from Hermes was saved to the inbox (plugin mode) |
| `session` | `id`, `title` | The conversation got a title, or changed |
| `chat` | `chat`, `session_title` | The whole conversation changed (new, or resumed): redraw it |
| `persona` | `persona` | The critter changed |
| `look` | `look` | The look changed: here, on another device, or by the agent. Redraw |
| `action` | `id`, `name`, `args` | Only to the client that offered it: run this and send `action.result` |

A turn always reads `user` → `turn.start` → (`status` / `tool` / `reply.delta`)* → `reply` →
`turn.end`. Write your UI against that and it works on both routes.

## The look

One settings file, kept on Hermes and shared by every device (`src/hermlet/look.py` has the full
description). An app applies what it can and ignores the rest.

| Key | Values |
|---|---|
| `theme` | `auto` (light by day, dark at night), `light`, `dark` |
| `accent`, `background`, `surface`, `text` | `#rrggbb`, or `null` to follow the theme (`accent`: the critter's colour) |
| `font` | `rounded`, `mono`, `serif`, `pixel`, or a path to a `.ttf` on the device |
| `text_size` | `small`, `medium`, `large`, or pixels from 10 to 40 |
| `corners` | `round`, `soft`, `square` |
| `fun` | `true`, or `false` for a plain look with no critter |
| `critter_size` | `small`, `medium`, `large` |
| `speak` | `true` to read replies aloud |
| `greeting` | a line for the home screen, or `null` |
| `fresh_after_min` | idle minutes before the next visit starts a fresh conversation; `0` never does. Default 20 |

`look.set` accepts colour words (`"red"`) and stores them as hex. The agent changes the same file
when asked to, through the `hermlet` skill; devices notice within seconds, and at once when the
change is made during a conversation on that device.

After `fresh_after_min` idle minutes the service starts a new conversation by itself and sends a
`chat` event with an empty list. The old conversation stays in the history.

## The persona

```json
{"version": 1, "seed": "9f2c...", "name": "Juso", "species": "bear",
 "palette": {"body": "#d888cc", "shade": "#c559c4", "outline": "#412343", "accent": "#e06788",
             "eye": "#20131e", "cheek": "#f28899", "white": "#ffffff"},
 "legend": {".": null, "b": "body", "s": "shade", "o": "outline", "a": "accent",
            "e": "eye", "c": "cheek", "w": "white"},
 "sprite": {"w": 16, "h": 16, "frames": {"idle": ["................", "..."], "blink": [...],
            "talk": [...], "think": [...], "happy": [...], "sleep": [...]}}}
```

Each frame is `h` strings of `w` legend characters; `.` is see-through. To draw it, look each
character up in `legend`, then that name in `palette`. No image decoder is needed, which is why
it fits on anything from a terminal (`hermlet critter`) to an LED matrix.

## From Python

`hermlet.connect()` wraps all of the above; see its docstrings (`src/hermlet/client.py`).

| Call | Does |
|---|---|
| `h.send(text, files=[...])` | Send, return at once |
| `h.ask(text, files=[...])` | Send and return the answer |
| `h.events()` | Iterate over events |
| `@h.on("reply")`, `h.run_forever()` | Call a function per event |
| `@h.action(name, description, params)` | Offer an action (plugin mode) |
| `h.sessions()`, `h.session_messages(id)`, `h.resume(id)` | History |
| `h.cancel()`, `h.new_session()`, `h.answer(id, text)` | Control |
| `h.talk_start()`, `h.talk_stop()` | Voice |
| `h.set_look(accent="red", font="mono")` | Restyle every device (or `this_device_only=True`) |
| `h.set_persona("Pib", "teal")`, `h.suggest_name()` | Name and colour the critter |
| `h.status`, `h.state`, `h.refresh()` | The snapshot |
| `h.request(op, **fields)` | Any op, by name |
