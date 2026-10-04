# Prompt: add the optional Hermlet plugin to my Hermes

Only needed if you want what the plugin adds (see `docs/two-ways.md`): the agent calling actions
on a device, reaching a device unprompted, and pairing codes instead of the dashboard password.
Paste everything below the line into a chat with your Hermes.

---

Please install the Hermlet platform plugin on this host so my Hermlet devices can connect through
it, and tell me what you did.

1. Install and enable it: `hermes plugins install <repository URL>/tree/main/plugin --enable`
   (the plugin is the `plugin/` directory of the Hermlet repository; its name is `hermlet`).
2. In `config.yaml`, turn the platform on. Bind it to this host's Tailscale address, not to every
   interface, so only my tailnet can reach it:

   ```yaml
   platforms:
     hermlet:
       enabled: true
       extra:
         host: <output of `tailscale ip -4`>
         port: 8790
         unauthorized_dm_behavior: pair
   ```
3. Restart the gateway so it loads the plugin. Tell me before you do, because it interrupts any
   conversation in progress on other platforms.
4. Check it: `curl -s http://<tailscale address>:8790/health` should answer `hermlet hub ok`, and
   `hermes hermlet info` should print the address devices connect to.

Then tell me the address to use on a device (`hermlet setup --plugin ws://<tailscale name>:8790/hermlet`).
When a device shows a pairing code I will ask you to run `hermes hermlet pair`; ask me to confirm
the device name before approving it.

**Do not:** bind the plugin to `0.0.0.0` on a machine with a public address, set
`HERMLET_ALLOW_ALL_USERS`, or approve a pairing code I have not confirmed.
