#!/usr/bin/env bash
# The local API needs no SDK: it is JSON lines on a Unix socket (docs/local-api.md).
# This sends one message and prints every event until the turn ends.
#
#   bash examples/raw_socket.sh "what time is it in Lisbon?"
set -euo pipefail
SOCK="${HERMLET_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/hermlet}/hermlet.sock"
TEXT="${1:-hello}"
python3 - "$SOCK" "$TEXT" <<'PY'
import json, socket, sys
s = socket.socket(socket.AF_UNIX)
s.connect(sys.argv[1])
f = s.makefile("rw")
print("hello:", json.loads(f.readline())["state"])
f.write(json.dumps({"op": "send", "text": sys.argv[2]}) + "\n")
f.flush()
for line in f:
    event = json.loads(line)
    print(event)
    if event["type"] == "turn.end":
        break
PY
