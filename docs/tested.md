# What has been run, and on what

Written down so you know what you are standing on. Everything here was run on 4 October 2026.

## On a Raspberry Pi 5

| | |
|---|---|
| Machine | Raspberry Pi 5 Model B Rev 1.1, 8 GB, no screen attached |
| System | Debian GNU/Linux 13 (trixie), kernel 6.18.34+rpt-rpi-2712, aarch64 |
| Toolchain | GCC 14.2.0, Python 3.13.5, CMake 4.4.4 (from pip) |
| Network | On the tailnet with Tailscale's DNS turned off; Hermes found through the fallback in `net.py` |
| Hermes | Hermes Agent 0.21.5 on another machine, dashboard behind `tailscale serve`, username and password |

| Measured | Result |
|---|---|
| Test suite (`pytest`, 47 tests at the time; 48 now) | all pass, 16.9 s |
| `install.sh --no-app --no-setup` | 11.4 s; `hermlet --version` works; `--uninstall --purge` leaves nothing behind |
| Virtualenv with the SDK, pytest and CMake | 20.4 s |
| App, `offscreen` backend: configure (fetches LVGL and cJSON) | 38 to 40 s |
| App, `offscreen` backend: build | 43.4 s with `-j3`, 45.1 s with `-j4` (the Pi was busy with other work), no warnings |
| App, `fbdev` backend: configure and build | 81.9 s, compiles and links |
| App binary | 1.1 MB, links only libc |
| App, start to first drawn frame | about 10 ms |
| App memory (800x480) | 4 MB |
| Service memory | 37 to 41 MB |
| `hermlet setup` (sign in, connect) | 2.3 s |
| A turn: typed question to finished answer | 3.3 s to 4.7 s on five turns, 14.2 s on a sixth (the time is the model's) |

What those runs covered (four passes, the last with the code as it is now): signing in with the
dashboard login, streamed replies, a text file attached and understood ("It's a Raspberry Pi 5
Model B with 8 GB of RAM, running Debian 13 on 64-bit ARM Linux"), `hermlet doctor`, and the app
drawing every screen in [docs/pi5](pi5) at 320x240, 480x320, 800x480 and 1280x720.

The Pi was left as it was found: no system packages were installed, the service was stopped, and
the test folder was removed.

### Not covered on the Pi

- **No screen.** The pictures were drawn by the `offscreen` build into memory. The `sdl` build
  (the one a desktop or a console with a monitor uses) was not built on the Pi, because that
  needs `libsdl2-dev` and the test did not install system packages. The `fbdev` build compiled
  but was never run.
- **No touch panel, keyboard or mouse.** Touch layouts were drawn, not touched.
- **No microphone, no speaker.** The Pi has neither. This is how it was found that the service
  offered a microphone button whenever `arecord` was installed; it now checks for a capture
  device first, and on the last pass the Pi correctly reported "microphone not found" and drew
  no microphone button. Recording and playback themselves have never been run.
- **`install.sh` with the app.** Only the SDK-only path was run. The app step installs `cmake`
  and `libsdl2-dev` with apt and has not been run on a Pi.
- **Smaller Pis.** Nothing was run on a Pi Zero, 3 or 4. The app and service are small (see the
  memory figures), but that is an expectation, not a result.

## Setting up from inside the app

Run on a laptop (macOS, the `offscreen` build), not yet on a Pi and not yet by hand on a screen:

- From an empty settings folder, opening the app started the service by itself; the service was
  answering 0.65 s later and the app showed "Where is your Hermes?".
- An address typed without `https://` or a port was resolved to the one that answered, saved,
  and led to the sign-in screen; a nonsense address was refused with a sentence under the form.
- From the same form to a working conversation against the development server, and then
  "Change address" to move the device to another Hermes (the automated test
  `test_the_app_can_do_the_whole_setup` covers the service side of all of this).
- The buttons were pressed by the app's own test hooks (`--screen connect:<address>`), not by a
  finger or a mouse. The service the app started was stopped afterwards.

## Against a live Hermes (0.21.5)

Run from a Mac and from the Pi:

- Sign-in, reconnecting with the saved session, streamed replies, a text file and an image
  attached and understood, listing and searching sessions from Discord, cron, the CLI, the TUI and
  the desktop app, reading one, continuing a Discord conversation as a new hermlet session with
  the original left unchanged.
- **Naming the critter:** stored on Hermes, picked up by a second device, and the agent, asked in
  a fresh conversation what its hermlet is called, answered with the name.
- **Asking the agent to restyle the app:** "Make the accent colour red and use a code-style font"
  changed the device 8.4 s into the turn, and Hermes's own skin was untouched.

One thing that went wrong on the way, worth knowing about: before each conversation carried a
note saying where it was happening, the same request made Hermes restyle *itself* (it changed its
own `display.skin` and `desktop.font_family`). That is what the hidden opening note
(`look.context`) is for. If you change that note, test this again.

Not run live: voice (that Hermes had no speech-to-text provider, and its text-to-speech provider
was not installed, so both endpoints answered with a clear error), approvals and questions from
the agent.

## On a Mac

The SDK and app were developed on macOS (Apple Silicon): the `sdl` and `offscreen` builds, 48
tests, and the pictures in [docs/images](images), which use the dev server's sample data. Dropping
a file onto the window was exercised by calling the drop handler directly, not by a real drag.

## The optional plugin

One end-to-end test (`tests/test_plugin_e2e.py`) against a real `hermes gateway run` from Hermes
Agent `main` (commit `af90026a`) with a scripted model: a device pairs through Hermes's own pairing
codes, gets a streamed reply, sends a file the agent receives, the agent turns a lamp on through
the device's action, the critter is named (stored, and written to the agent's notes through
Hermes's memory store), and the device reads its conversation from Hermes's session store.

Not tested with the plugin: voice, approvals, cron delivery, continuing another surface's
conversation, the shared look, `hermes hermlet pair` and `hermes hermlet persona`, and any Hermes
version other than that commit.
