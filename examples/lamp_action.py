"""Give your agent something it can do on this device.

Actions need the Hermlet plugin on your Hermes (docs/two-ways.md explains
why): Hermes's own web API has no way for the agent to call back into a device.
With the plugin, "turn the desk lamp on" from Discord reaches this function.

    python examples/lamp_action.py
"""
import hermlet

h = hermlet.connect()
state = {"on": False}


@h.action("lamp.set", "Turn the desk lamp on or off.",
          {"type": "object", "properties": {"on": {"type": "boolean", "description": "true for on"}}, "required": ["on"]})
def lamp_set(on: bool):
    # from gpiozero import LED; LED(27).value = on
    state["on"] = bool(on)
    return state


@h.action("lamp.get", "Whether the desk lamp is on.")
def lamp_get():
    return state


print("offering lamp.set and lamp.get to the agent; Ctrl+C to stop")
h.run_forever()
