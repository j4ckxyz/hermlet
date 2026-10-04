# Prompt: set up Hermlet on this device

For a coding agent with a shell on the Raspberry Pi (or other Linux machine) that should become a
hermlet. Paste everything below the line.

---

Set up Hermlet on this machine and connect it to my Hermes. Work through these steps, show me
what each check printed, and stop to ask when a step needs something only I can give.

1. **Look first.** `uname -a`, `cat /etc/os-release`, `free -m`, `python3 --version`. Hermlet
   needs Linux and Python 3.9 or later. Note whether there is a desktop session
   (`echo $XDG_SESSION_TYPE $DISPLAY $WAYLAND_DISPLAY`) or only a console.
2. **Tailscale.** `tailscale status`. If it is not installed, install it the way
   https://tailscale.com/download/linux says for this distribution, then ask me to log in with
   `sudo tailscale up` (I have to open the link myself). If my Hermes is on the same local
   network instead, Tailscale can be skipped.
3. **Reach Hermes.** Ask me for my Hermes address (it looks like
   `https://hermes.my-tailnet.ts.net`). Check `curl -s <address>/api/status`: it should return
   JSON with a `version`. If it does not, the problem is on the Hermes side or the network; do
   not go on until this works.
4. **Install.** In a checkout of the Hermlet repository: `bash install.sh --no-setup`. It asks
   before installing system packages. On a machine with under 1 GB of memory the app takes a few
   minutes to build; if the build fails for lack of memory, rerun with `--no-app` and tell me
   (`hermlet chat` works without the app).
5. **Connect.** If I am at the device's own screen, I can simply open the app (`hermlet-app`):
   it asks for the address and my sign-in itself, and you can skip to step 7. Otherwise run
   `hermlet setup <address> --name "<a name I choose>"`. It asks for my Hermes
   dashboard username and password: let me type them myself, do not ask me to paste the password
   into this chat, and never write it to a file. Only the signed-in session is stored
   (`~/.config/hermlet/auth.json`, mode 0600).
6. **Check.** `hermlet doctor` should end with "all good". `hermlet send "say hello in five words"`
   should print a reply.
7. **Keep it running.** `hermlet service install` (a systemd user service, started at boot).
8. **The app.** With a desktop: it is in the menu as "Hermlet", or run `hermlet-app`;
   `bash install.sh --autostart` opens it at login. With only a console: run `hermlet-app` on the
   machine's own screen (not over SSH); to open it at boot, add `hermlet-app` to the end of
   `~/.bash_profile` and enable console auto-login with `sudo raspi-config`.
9. **Voice (optional).** `arecord -l` lists microphones. If there is one, `hermlet doctor` shows
   the microphone as ready. If the wrong device is used, set `"mic": "plughw:1,0"` (the card and
   device numbers from `arecord -l`) in `~/.config/hermlet/config.json` and restart the service.

If something fails, `hermlet doctor` and `journalctl --user -u hermlet -e` say why. Do not work
around a failure by turning off authentication on Hermes or exposing it to the internet.
