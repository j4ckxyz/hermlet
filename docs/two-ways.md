# Two ways to reach Hermes

A hermlet can connect to Hermes by either of two routes. The SDK, the local API and the app are
the same on both; only the connection underneath differs. The address you give `hermlet setup`
picks the route (`https://...` is the dashboard, `ws://.../hermlet` is the plugin).

## The short version

**Use the dashboard route.** It needs no software installed on Hermes, it covers everything most
people want, and it is the one that has been run on real hardware against a real Hermes. Add the
plugin only if you want your agent to *act on* a device.

## What each can do

| | Dashboard API (default) | Plugin (optional) |
|---|---|---|
| Installed on Hermes | No software. Three small files and one line of the agent's notes are written through Hermes's own API | The `hermlet` platform plugin, and a gateway restart |
| What must be running on Hermes | `hermes dashboard`, with a username and password | `hermes gateway` |
| How a device is trusted | Signs in with the dashboard login once; keeps a session that Hermes rotates | A pairing code the owner approves; the device proves a key it made itself |
| Typed messages, streamed replies | Yes | Yes |
| Files and pictures to the agent | Yes (10 MB each) | Yes (25 MB each) |
| Voice in (speech-to-text by Hermes) | Written; not yet run against a Hermes with speech-to-text | Written; not yet tested with Hermes |
| Spoken replies | Written; not yet run against a Hermes with text-to-speech | Partly written (WAV only); not tested |
| History from Discord, CLI, web, other hermlets | Yes, with full-text search | Yes |
| Continue another surface's conversation here | Yes | Written, not yet tested |
| Tool activity while it works | Yes, each tool call | A status line |
| Approvals and questions from the agent | Yes (tested against a stand-in only) | Written, not yet tested |
| Name and colour the critter; the agent knows | Yes | Yes |
| Shared look; "make the accent red" | Yes | Written, not yet tested with Hermes |
| Fresh conversation after idle time | Yes | Yes |
| **The agent calls an action on the device** | No | **Yes** (`hermlet_action`) |
| **The agent reaches the device unprompted** (cron results, "tell the kitchen Pi...") | No | **Yes** |
| **The agent sends a file or a card to the device** | No | **Yes** |

The three bold rows are the whole case for the plugin. They are things the dashboard API was not
made for: it is a client-driven API, so Hermes has no way to start a conversation with a device
or call into it.

## What the dashboard route writes on Hermes

Nothing is installed, but it does leave these, all through the dashboard's own file API and only
once you name your hermlet:

| What | Where (under the Hermes home) | Why |
|---|---|---|
| The critter's name and colour | `plugin-data/hermlet/persona.json` | So every device shows the same one |
| The app's look | `plugin-data/hermlet/look.json` | So every device looks the same, and the agent can change it |
| A skill | `skills/hermlet/SKILL.md` | Tells the agent what the look file's keys mean |
| One entry in the agent's notes | `memories/MEMORY.md` | So Hermes knows its critter's name on every surface |

Delete them and Hermes is as it was. Each conversation started on a device also opens with a short
hidden note (about 110 tokens) saying it is happening on a hermlet.

## What the dashboard route costs you

- **A device holds a dashboard session.** The Hermes dashboard is an admin surface: someone with
  that session can read and change your Hermes configuration, including API keys. The password
  itself is never stored (only `auth.json`, mode 0600, holding an access token Hermes rotates
  every 12 hours and a refresh token that lapses after 30 days unused), but a stolen SD card is
  a stolen session until you change the dashboard secret
  (`HERMES_DASHBOARD_BASIC_AUTH_SECRET`), which signs every device out. Hermes has no narrower
  credential to give a device today. The plugin's per-device key grants only "talk to the agent
  as this device", and one device can be revoked without touching the others.
- **It follows Hermes's internal API.** `/api/ws` is the protocol Hermes's own desktop app uses.
  It is described by a generated contract in the Hermes repository, but it is not promised to
  stay still the way a public API would be. The SDK keeps every use of it in one file
  (`src/hermlet/dashboard.py`).
- **A separate process from your messaging gateway.** `hermes dashboard` and `hermes gateway`
  (Discord, Telegram) are two processes sharing one session store. That is why continuing a
  Discord conversation on a device copies it into a new session rather than writing into the
  one Discord is using.

## What the plugin route costs you

- Something to install, enable and keep working across Hermes updates. It registers through
  Hermes's documented platform-plugin interface, but leans on a few behaviours that are
  conventions rather than contracts (how a pairing code is obtained, how an approval is noticed).
- A second port to open on your tailnet (8790).
- A gateway restart to install it.

## What was tested

See [tested.md](tested.md).

## Where this could go

The cleanest way to get device actions *without* a plugin would be for the service to offer a
device's actions as an MCP server on the tailnet and register it with Hermes through the
dashboard's own MCP settings. Hermes already knows how to call MCP tools, from every surface.
That is not built.
