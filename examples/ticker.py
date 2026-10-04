"""Watch everything that happens on this hermlet, as it happens.

Useful as a starting point for your own display: an e-ink panel, an LED
matrix, a status light. Every event is a small dictionary with a "type".

    python examples/ticker.py
"""
import hermlet

h = hermlet.connect()
print("state:", h.state, "| critter:", (h.status.get("persona") or {}).get("name"))
for event in h.events():
    kind = event["type"]
    if kind == "user":
        print("you   :", event["text"])
    elif kind == "status" and event.get("text"):
        print("       (" + event["text"] + ")")
    elif kind == "reply":
        print("hermes:", event["text"])
    elif kind == "link":
        print("link  :", event["state"], event.get("error", ""))
    elif kind == "file":
        print("file  :", event["path"])
