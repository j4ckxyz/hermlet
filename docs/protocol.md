# Hermlet plugin protocol, version 1

The contract between a device and the optional Hermlet plugin inside the Hermes gateway. (The
default route, Hermes's own dashboard API, is not described here: it is Hermes's protocol, and
`src/hermlet/dashboard.py` is the only place that speaks it.)

The device side is `src/hermlet/device.py`; the server side is `plugin/hub.py`. `hermlet devserver`
runs the same hub with a scripted brain, so a device in another language can be developed against
it with no Hermes.

## Transport

- One WebSocket per device: `ws://<hermes-host>:8790/hermlet`. Over Tailscale the link is already
  encrypted; there is no TLS in the hub itself.
- Subprotocol `hermlet.v1`. Devices should offer it; the hub also accepts connections that do not.
- A plain `GET /health` on the same port answers `hermlet hub ok`, for setup tools.
- **Text frames** are UTF-8 JSON objects with a `"type"`. Fields that are not needed are left out,
  never sent as `null`.
- **Binary frames** carry file and audio bytes behind a four-byte header:

  | Offset | Size | Field |
  |---|---|---|
  | 0 | u8 | channel: `0x01` audio, `0x02` file |
  | 1 | u8 | stream id, from the matching `*.start` |
  | 2 | u16 LE | sequence number (wraps) |
  | 4 | ... | payload |

  No single frame may exceed 1 MB; files go in 32 KB pieces.

## Handshake

```
device                                   hub
  | -- hello ------------------------------> |   identity, capabilities, actions
  | <------------------------- challenge --- |   nonce, enrolled?
  | -- auth -------------------------------> |   key (first contact) or HMAC
  | <--------------------------- welcome --- |   paired?, persona, limits
  | <------------- pairing (when unpaired) - |   code + the command that approves it
  | <------------- paired (after approval) - |
```

**Identity.** A device makes a random 32-byte key on first run. Its id is derived from it:
`"hl-" + hex(sha256(key))[0:16]`. Nobody can claim an id without that device's key.

```json
{"type": "hello", "proto": 1, "device_id": "hl-630dcd2966c43366", "name": "Kitchen Pi",
 "kind": "linux-aarch64", "sdk": "0.1.0", "token": "<only if the hub requires one>",
 "caps": {"text": {"markdown": false}, "files": true, "sessions": true,
          "mic": {"rate": 16000, "format": "pcm16"}, "speaker": {"format": "pcm16"},
          "display": {"width": 800, "height": 480}},
 "actions": [{"name": "lamp.set", "description": "Turn the desk lamp on or off.",
              "params": {"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]}}],
 "sensors": {"temperature_c": 21.5}}
```

```json
{"type": "challenge", "nonce": "<base64, 16 bytes>", "enrolled": true}
```

- **First contact** (`enrolled: false`): `{"type": "auth", "key": "<base64 key>"}`. The hub
  checks the key hashes to the id and keeps it. The key crosses the network this once.
- **Afterwards**: `{"type": "auth", "mac": "<base64>"}` with
  `mac = HMAC-SHA256(key, "hermlet/v1|" + device_id + "|" + nonce)`.
- A device remembers which hubs it has enrolled with and refuses to send its key to one of them
  again, so a hub that "forgets" a device cannot talk it out of the key
  (`hermlet setup --re-enroll` overrides this on purpose).

```json
{"type": "welcome", "proto": 1, "session": "3f2a9c1b7d4e", "paired": false, "heartbeat_s": 20,
 "agent": "Hermes", "persona": {...}, "limits": {"upload_bytes": 25000000, "utterance_s": 120}}
```

`paired` says whether Hermes accepts messages from this device. Until it does, every message
except `actions`, `action.result` and `state` is answered with `error` / `not_paired`.

Errors during the handshake are `{"type": "error", "code": "...", "message": "..."}` followed by
a close: `bad_hello`, `bad_proto`, `bad_device_id`, `bad_token`, `auth_failed`.

## Device to hub

| Message | Meaning |
|---|---|
| `{"type":"text","id":"m1","text":"...","attachments":["u1"]}` | A message. `attachments` names uploads already acknowledged |
| `{"type":"upload.start","id":"u1","stream":3,"name":"photo.jpg","mime":"image/jpeg","size":48213}` | A file follows on binary channel 2 |
| `{"type":"upload.end","id":"u1","stream":3,"sha256":"..."}` | The file is complete. Answered with `upload.ok` or `upload.error` |
| `{"type":"audio.start","id":"a1","stream":4,"rate":16000,"format":"pcm16"}` | A spoken message follows on binary channel 1 (mono PCM16) |
| `{"type":"audio.end","id":"a1","stream":4}` | Done: the hub hands Hermes a WAV voice note |
| `{"type":"audio.cancel","stream":4}` | Throw it away |
| `{"type":"cancel"}` | Stop the current turn (Hermes `/stop`) |
| `{"type":"session.new"}` | A fresh conversation (Hermes `/new`) |
| `{"type":"prompt.reply","id":"q1","answer":"yes"}` | The answer to a `prompt` |
| `{"type":"sessions.list","id":"s1","limit":20,"offset":0,"source":"discord","query":"train"}` | Ask for past conversations |
| `{"type":"sessions.get","id":"s2","session_id":"...","limit":50}` | Ask for one conversation's messages |
| `{"type":"sessions.resume","id":"s3","session_id":"..."}` | Carry that conversation on here |
| `{"type":"persona.set","id":"p1","name":"Pib","colour":"teal"}` | Name and colour the critter. Answered with `persona.result`; every device then gets `persona` |
| `{"type":"look.set","id":"l1","changes":{"accent":"red"},"device":"Kitchen Pi"}` | Change the shared look (`device` limits it to one). Answered with `look.result`; every device then gets `look` |
| `{"type":"actions","actions":[...]}` | Replace the action manifest (apps come and go) |
| `{"type":"action.result","id":"x1","ok":true,"result":{...}}` | The answer to an `action`; `ok:false` carries `error` |
| `{"type":"state","sensors":{"temperature_c":21.5}}` | Latest readings |
| `{"type":"event","name":"door.opened","data":{...},"notify":true}` | With `notify`, the agent is told as a message |

## Hub to device

| Message | Meaning |
|---|---|
| `pairing` `{code, command}` | Show this code; the owner approves it on the Hermes host |
| `paired` / `unpaired` | Approved (no reconnect needed) / approval revoked |
| `persona` `{persona}` | The agent's critter changed |
| `persona.result` `{id, persona, memory}` | The answer to `persona.set`; `memory` is `saved`, `full`, `off` or `failed` |
| `look` `{look}` | The shared look file changed (also sent in `welcome`) |
| `look.result` `{id, look}` | The answer to `look.set` |
| `turn.start` `{turn}` | Hermes started working |
| `status` `{text}` | A working-state phrase; empty clears it |
| `reply.delta` `{turn, text}` | The reply so far (cumulative) |
| `reply` `{turn, text, interim}` | A reply; `interim` marks commentary before the answer |
| `turn.end` `{turn, outcome}` | `success`, `failure` or `cancelled` |
| `transcript` `{text}` | What speech-to-text heard |
| `notice` `{text, ttl_s}` | A short message to show for a while |
| `card` `{title, body, ttl_s}` | A card from the agent (`hermlet_show`) |
| `prompt` `{id, title, text, choices, kind}` | A question; answer with `prompt.reply` |
| `prompt.close` `{id}` | Withdrawn |
| `upload.ok` `{id, size}` / `upload.error` `{id, error}` | The fate of an upload |
| `file.start` `{stream, name, mime, size, caption, turn}` | A file follows on binary channel 2 |
| `file.end` `{stream, sha256}` | The file is complete |
| `audio.start` `{stream, rate, channels, format, turn}` | Speech follows on binary channel 1, paced at most 0.5 s ahead of real time |
| `audio.end` `{stream}` / `audio.abort` `{stream}` | Finished / stop now |
| `action` `{id, name, args}` | Run this action; answer once with `action.result` within 20 s |
| `sessions.result` `{id, op, ...}` | `sessions` for a list; `session` and `messages` for a get; `session` for a resume; or `error` |
| `error` `{code, message, id}` | Something about a message was wrong (`not_paired`, `missing_upload`, `bad_json`) |

Reply text has had Markdown reduced to plain text unless the device said
`caps.text.markdown: true`.

## How it sits in Hermes

Each device is a direct-message chat whose `chat_id` and `user_id` are the device id, on a
platform named `hermlet`. Sessions, memory, `/new`, `/stop`, pairing codes, approvals and cron
delivery (`hermlet:<device-id>`) are Hermes's own, unchanged. The agent gets three tools:
`hermlet_devices`, `hermlet_action` and `hermlet_show`.
