"""The optional plugin against a real Hermes: ``hermes gateway run`` with the plugin installed,
a real HubDevice, and a fake OpenAI-compatible server standing in for the model.

Opt-in (spawns a gateway, about a minute):

    HERMLET_E2E=1 HERMES_AGENT_DIR=/path/to/hermes-agent pytest tests/test_plugin_e2e.py

``HERMES_AGENT_DIR`` is a Hermes Agent checkout with a virtualenv in ``.venv``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from conftest import REPO, running, until
from hermlet import HubDevice

AGENT_DIR = Path(os.environ.get("HERMES_AGENT_DIR", REPO.parent / "hermes-agent"))
VENV_PY = AGENT_DIR / ".venv" / "bin" / "python"

pytestmark = [
    pytest.mark.skipif(os.environ.get("HERMLET_E2E") != "1", reason="set HERMLET_E2E=1 to run"),
    pytest.mark.skipif(not VENV_PY.exists(), reason=f"no Hermes virtualenv at {VENV_PY}"),
]

CONFIG = """\
model:
  provider: custom
  default: fake-hermlet-model
  base_url: http://127.0.0.1:{api}/v1
  api_key: test-key
platforms:
  hermlet:
    enabled: true
    extra:
      host: 127.0.0.1
      port: {port}
      unauthorized_dm_behavior: pair
plugins:
  enabled: [hermlet]
streaming:
  enabled: true
stt:
  enabled: true
  provider: openai
  openai: {{api_key: test-key, base_url: "http://127.0.0.1:{api}/v1", model: whisper-1}}
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _hermes(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "HERMES_HOME": str(home), "PYTHONUTF8": "1"}
    return subprocess.run([str(VENV_PY), "-m", "hermes_cli.main", *args], cwd=AGENT_DIR, env=env,
                          capture_output=True, text=True, timeout=180)


@pytest.fixture
def gateway(short_tmp):
    from fakes import openai_api

    api_server, api_port = openai_api.start(0)
    port = _free_port()
    home = short_tmp / "hermes-home"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(REPO / "plugin", home / "plugins" / "hermlet", ignore=shutil.ignore_patterns("__pycache__"))
    (home / "config.yaml").write_text(CONFIG.format(api=api_port, port=port), encoding="utf-8")
    env = {**os.environ, "HERMES_HOME": str(home), "PYTHONUTF8": "1"}
    log_path = short_tmp / "gateway.out"
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([str(VENV_PY), "-m", "hermes_cli.main", "gateway", "run"], cwd=AGENT_DIR, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        if proc.poll() is not None:
            pytest.fail(f"gateway exited: {log_path.read_text(errors='replace')[-3000:]}")
        time.sleep(0.5)
    else:
        proc.kill()
        pytest.fail(f"gateway did not open the hermlet port: {log_path.read_text(errors='replace')[-3000:]}")
    yield {"url": f"ws://127.0.0.1:{port}/hermlet", "home": home, "api": openai_api.Handler.requests_log, "log": log_path}
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()
    api_server.shutdown()
    if os.environ.get("HERMLET_E2E_LOG"):  # debugging aid: what the gateway said
        print(log_path.read_text(errors="replace")[-int(os.environ["HERMLET_E2E_LOG"]):])


def test_a_device_pairs_talks_sends_a_file_and_is_driven_by_the_agent(gateway, run, short_tmp):
    async def scenario():
        device = HubDevice(gateway["url"], "E2E Pi", home=short_tmp / "pi")
        device.cfg["inbox"] = str(short_tmp / "inbox")
        lamp = {"on": False}

        @device.action("lamp.set", "Turn the desk lamp on or off.",
                       {"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]})
        def lamp_set(on: bool):
            lamp["on"] = bool(on)
            return lamp

        seen = []
        device.on("*")(seen.append)
        async with running(device):
            # Pairing goes through Hermes's own DM pairing codes.
            await until(lambda: device.pairing, timeout=60, what="a pairing code from Hermes")
            approved = await asyncio.to_thread(_hermes, gateway["home"], "pairing", "approve", "hermlet", device.pairing["code"])
            assert approved.returncode == 0, approved.stdout + approved.stderr
            await device.wait_ready(30)

            # Text in, the model's reply out, streamed.
            reply = await device.ask("hello hermes", timeout=120)
            assert "Echo: hello hermes" in reply
            assert any(m["type"] == "reply.delta" for m in seen)

            # A file rides along and reaches the agent.
            reply = await device.ask("what is in this note?", [("note.txt", b"oat milk and lemons")], timeout=120)
            assert "what is in this note?" in reply
            assert any("note" in r["raw_last"] for r in gateway["api"]), gateway["api"][-1]

            # The agent calls an action on the device.
            reply = await device.ask("please turn the lamp on", timeout=120)
            assert lamp["on"] is True, reply
            assert "lamp is on" in reply

            # Naming the critter: kept in Hermes's plugin data, and written into the agent's own notes
            # through Hermes's memory store.
            result = await device.set_persona("Pib", "teal")
            assert result["persona"]["name"] == "Pib" and result["persona"]["colour"] == "#4fb3a6"
            assert result["memory"] == "saved", result
            stored = gateway["home"] / "plugin-data" / "hermlet" / "persona.json"
            assert '"name": "Pib"' in stored.read_text()
            assert "named Pib, a teal" in (gateway["home"] / "memories" / "MEMORY.md").read_text()

            # History is Hermes's own session store.
            sessions = await device.sessions()
            assert any(s["source"] == "hermlet" and s["current"] for s in sessions), sessions
            current = next(s for s in sessions if s["current"])
            messages = (await device.session_messages(current["id"]))["messages"]
            assert any("hello hermes" in m["content"] for m in messages if m["role"] == "user")
    run(scenario(), timeout=420)
