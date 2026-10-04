"""Let your agent run commands on this device. Read this before you use it.

This hands your agent a shell on the machine, as the account running this
script. It is off by default for a reason: anyone who can talk to your agent
(a Discord server it is in, a prompt injected through a web page it reads) may
be able to make it run something here. Run it as an account with no sudo and
nothing precious, or do not run it.

Needs the Hermlet plugin on your Hermes (see docs/two-ways.md).

    python examples/shell_action.py
"""
import subprocess

import hermlet

h = hermlet.connect()


@h.action("system.run", "Run a shell command on this device and return its output.",
          {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]})
def run(command: str):
    done = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=15)
    return {"exit_code": done.returncode, "stdout": done.stdout[-4000:], "stderr": done.stderr[-2000:]}


print("the agent can now run commands here as this account; Ctrl+C to stop")
h.run_forever()
