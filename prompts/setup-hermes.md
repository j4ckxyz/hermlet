# Prompt: get my Hermes ready for Hermlet devices

Paste everything below the line into a chat with your Hermes (Discord, the CLI, anywhere it can
run commands on its own host).

---

I want to connect small devices (Raspberry Pis running the Hermlet SDK) to you. They talk to the
Hermes dashboard's own API, the same way the Hermes desktop app connects to a remote backend, so
nothing new needs installing. Please get this host ready and tell me what you did.

**The goal.** From another machine on my Tailscale network, this must work:

```sh
curl -s https://<this-host>.<my-tailnet>.ts.net/api/status
```

and the JSON it returns must contain `"auth_required": true` and `"basic"` in `auth_providers`.
(A plain `http://<tailscale-name>:9119` address is fine too if HTTPS is not set up.)

**What that takes.** Check each of these and fix only what is missing:

1. **Tailscale** is installed and this host is logged in (`tailscale status`). If it is not,
   stop and tell me: I have to log in myself.
2. **The dashboard has a username and password.** That is the `HERMES_DASHBOARD_BASIC_AUTH_USERNAME`,
   `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD` and `HERMES_DASHBOARD_BASIC_AUTH_SECRET` settings in your
   `.env` (the secret is 32 or more random bytes, for example from `openssl rand -base64 32`).
   If they are not set, ask me for the username and password I want. Do not invent a password
   and do not print an existing one back to me.
3. **`hermes dashboard` runs all the time** and survives a reboot (a systemd service is the
   usual way), listening somewhere my tailnet can reach and never on the public internet. Your
   own documentation covers this under "Web Dashboard" → "Connecting Hermes Desktop to a remote
   backend"; follow that, including the part about the Host header having to match the address
   I will use.
4. **The address is reachable over Tailscale.** Either the dashboard listens on this host's
   Tailscale address, or `tailscale serve` publishes it on the tailnet over HTTPS. Use
   `tailscale serve`, not `tailscale funnel`: funnel would put it on the public internet.
5. **Optional, for voice:** to talk to my devices, speech-to-text has to work (`stt` in
   config.yaml); to have answers read aloud, text-to-speech has to work (`tts`). Check both by
   asking the dashboard: `POST /api/audio/transcribe` and `POST /api/audio/speak` say plainly what
   is missing (for example "Enable Edge TTS with: hermes pm install --extra edge-tts"). Tell me
   what each needs; free local options are fine to set up, but ask before a paid provider.

**When you are done**, run the `curl` above from this host against the Tailscale address and show
me the two fields. Then tell me the exact address to type into `hermlet setup` on my device.

**Do not:** expose the dashboard to the internet, turn authentication off, change my model or
provider settings, or restart the messaging gateway unless one of the steps above needs it (say
so first if it does).
