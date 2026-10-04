"""The device side against the real hub: handshake, pairing, messages, files, voice, actions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import struct

import pytest

from conftest import dev_hub, running, until
from hermlet import Device, HermletError, protocol


def make_device(url, home, **kw):
    return Device(url, kw.pop("name", "Test Pi"), home=home, **kw)


def test_first_contact_enrolls_and_later_contacts_prove_identity(run, short_tmp):
    async def scenario():
        async with dev_hub(state_dir=str(short_tmp / "hub")) as (hub, brain, url):
            async with running(make_device(url, short_tmp / "a")) as device:
                await device.wait_ready(10)
                assert device.agent == "Dev Hermes"
                assert device.persona["sprite"]["frames"]["idle"]
                assert hub.store.key_for(device.device_id) == device.identity.key
            # Same identity again: answers the challenge with an HMAC, never the key.
            async with running(make_device(url, short_tmp / "a")) as device:
                await device.wait_ready(10)
                assert device.identity.is_enrolled(url)
    run(scenario())


def test_a_stolen_device_id_without_the_key_is_refused(run, short_tmp):
    async def scenario():
        async with dev_hub(state_dir=str(short_tmp / "hub")) as (hub, brain, url):
            async with running(make_device(url, short_tmp / "a")) as device:
                await device.wait_ready(10)
                victim = device.device_id
            from websockets.asyncio.client import connect
            async with connect(url) as ws:
                await ws.send(protocol.message("hello", proto=1, device_id=victim, name="Imposter"))
                challenge = json.loads(await ws.recv())
                assert challenge["enrolled"] is True
                await ws.send(protocol.message("auth", mac=protocol.mac_for(b"x" * 32, victim, challenge["nonce"])))
                assert json.loads(await ws.recv())["code"] == "auth_failed"
    run(scenario())


def test_a_hub_that_forgot_the_device_does_not_get_the_key_again(run, short_tmp):
    async def scenario():
        async with dev_hub(state_dir=str(short_tmp / "hub")) as (hub, brain, url):
            async with running(make_device(url, short_tmp / "a")) as device:
                await device.wait_ready(10)
            hub.store.forget(device.device_id)
            errors = []
            again = make_device(url, short_tmp / "a")
            again.on("link")(lambda m: errors.append(m.get("error")))
            async with running(again):
                await until(lambda: any(e and "no longer knows" in e for e in errors), what="refusal to re-enroll")
            assert hub.store.key_for(again.device_id) is None
    run(scenario())


def test_wrong_access_token_is_refused(run, short_tmp):
    async def scenario():
        async with dev_hub(token="s3cret") as (hub, brain, url):
            errors = []
            device = make_device(url, short_tmp / "a", token="nope")
            device.on("link")(lambda m: errors.append(m.get("error")))
            async with running(device):
                await until(lambda: any(e and "access token" in e for e in errors), what="token refusal")
            async with running(make_device(url, short_tmp / "b", token="s3cret")) as ok:
                await ok.wait_ready(10)
    run(scenario())


def test_pairing_code_then_approval(run, short_tmp):
    async def scenario():
        async with dev_hub(require_pairing=True) as (hub, brain, url):
            async with running(make_device(url, short_tmp / "a")) as device:
                await until(lambda: device.pairing, what="a pairing code")
                assert device.state == "pairing"
                with pytest.raises(HermletError):
                    await device.send("too early")
                assert await brain.approve(device.pairing["code"])
                await device.wait_ready(10)
                assert await device.ask("hello") == "Echo: hello"
    run(scenario())


def test_ask_streams_and_returns_the_reply(run, short_tmp):
    async def scenario():
        async with dev_hub() as (hub, brain, url):
            device = make_device(url, short_tmp / "a")
            seen = []
            device.on("*")(lambda m: seen.append(m["type"]))
            async with running(device):
                await device.wait_ready(10)
                assert await device.ask("good morning hermes") == "Echo: good morning hermes"
            assert seen.index("turn.start") < seen.index("reply.delta") < seen.index("reply") < seen.index("turn.end")
    run(scenario())


def test_files_and_images_arrive_intact(run, short_tmp):
    async def scenario():
        photo = short_tmp / "photo.png"
        payload = bytes(range(256)) * 700  # about 180 KB: several binary frames
        photo.write_bytes(payload)
        async with dev_hub() as (hub, brain, url):
            async with running(make_device(url, short_tmp / "a")) as device:
                await device.wait_ready(10)
                reply = await device.ask("what is this?", [photo, ("notes.txt", b"remember the milk")])
        assert "2 file(s)" in reply
        assert f"photo.png (image/png, {len(payload)} bytes, sha256 {hashlib.sha256(payload).hexdigest()[:12]})" in reply
        assert "notes.txt (text/plain, 17 bytes" in reply
    run(scenario())


def test_oversized_upload_is_refused_before_sending(run, short_tmp):
    async def scenario():
        async with dev_hub() as (hub, brain, url):
            hub.max_upload_bytes = 1000
            async with running(make_device(url, short_tmp / "a")) as device:
                await device.wait_ready(10)
                device.limits = {}  # pretend the device does not know the limit: the hub must still refuse
                with pytest.raises(HermletError, match="did not accept"):
                    await device.send("big", [("big.bin", b"x" * 5000)])
    run(scenario())


def test_file_from_the_agent_lands_in_the_inbox(run, short_tmp):
    async def scenario():
        async with dev_hub() as (hub, brain, url):
            device = make_device(url, short_tmp / "a")
            device.cfg["inbox"] = str(short_tmp / "inbox")
            files = []
            device.on("file")(files.append)
            async with running(device):
                await device.wait_ready(10)
                await device.ask("send me a file")
                await until(lambda: files, what="the file")
        assert files[0]["caption"] == "A file for you"
        assert (short_tmp / "inbox" / "hello.txt").read_text() == "Hello from the dev server.\n"
    run(scenario())


def test_voice_message_is_assembled_into_a_wav(run, short_tmp):
    async def scenario():
        pcm = b"".join(struct.pack("<h", int(8000 * math.sin(i / 8))) for i in range(16000))
        async with dev_hub() as (hub, brain, url):
            device = make_device(url, short_tmp / "a", mic=True)
            heard = []
            device.on("transcript")(heard.append)
            done = asyncio.Event()
            device.on("turn.end")(lambda m: done.set())
            async with running(device):
                await device.wait_ready(10)
                async with device.voice(16000) as v:
                    for i in range(0, len(pcm), 3200):
                        await v.write(pcm[i:i + 3200])
                await asyncio.wait_for(done.wait(), 10)
        assert heard and "1.0 seconds" in heard[0]["text"]
    run(scenario())


def test_agent_calls_a_device_action(run, short_tmp):
    async def scenario():
        async with dev_hub() as (hub, brain, url):
            device = make_device(url, short_tmp / "a")
            calls = []

            @device.action("lamp.toggle", "Toggle the lamp.")
            async def toggle():
                calls.append("toggle")
                return {"on": True}

            @device.action("lamp.break", "Always fails.")
            def broken():
                raise RuntimeError("bulb is gone")

            async with running(device):
                await device.wait_ready(10)
                assert [a["name"] for a in hub.get(device.device_id).actions] == ["lamp.toggle", "lamp.break"]
                assert await device.ask("action lamp.toggle") == "lamp.toggle returned {'on': True}"
                assert await device.ask("action lamp.break") == "lamp.break failed: bulb is gone"
                assert "no action named" in await device.ask("action nope")

                # Actions added after connecting are announced too.
                device.add_action("late.one", lambda: "ok", "Added later.")
                await until(lambda: len(hub.get(device.device_id).actions) == 3, what="the new action")
            assert calls == ["toggle"]
    run(scenario())


def test_history_from_other_surfaces_can_be_browsed(run, short_tmp):
    async def scenario():
        async with dev_hub() as (hub, brain, url):
            async with running(make_device(url, short_tmp / "a")) as device:
                await device.wait_ready(10)
                await device.ask("remember this")
                sessions = await device.sessions()
                assert {s["source"] for s in sessions} == {"hermlet", "discord", "cli"}
                assert [s["id"] for s in await device.sessions(source="discord")] == ["dev-discord"]
                assert [s["id"] for s in await device.sessions(query="backup")] == ["dev-cli"]
                discord = await device.session_messages("dev-discord")
                assert [m["role"] for m in discord["messages"]] == ["user", "assistant"]
                current = await device.session_messages("dev-current")
                assert current["messages"][0]["content"] == "remember this"
                with pytest.raises(HermletError, match="no such conversation"):
                    await device.session_messages("nope")
    run(scenario())


def test_reconnects_after_the_hub_restarts(run, short_tmp):
    async def scenario():
        from hermlet import devserver

        hub, brain = await devserver.serve(state_dir=str(short_tmp / "hub"), word_delay=0)
        port = hub.bound_port
        device = make_device(f"ws://127.0.0.1:{port}/hermlet", short_tmp / "a")
        states = []
        device.on("link")(lambda m: states.append(m["state"]))
        async with running(device):
            await device.wait_ready(10)
            await hub.stop()
            await until(lambda: device.state != "ready", what="noticing the drop")
            hub, brain = await devserver.serve(state_dir=str(short_tmp / "hub"), port=port, word_delay=0)
            try:
                await device.wait_ready(15)
                assert await device.ask("still there?") == "Echo: still there?"
            finally:
                await hub.stop()
        assert states.count("ready") == 2
    run(scenario(), timeout=45)


def test_url_forms_people_type():
    n = protocol.normalize_url
    assert n("hermes-box") == "ws://hermes-box:8790/hermlet"
    assert n("hermes-box.tail1234.ts.net:9000") == "ws://hermes-box.tail1234.ts.net:9000/hermlet"
    assert n("http://100.64.0.7") == "ws://100.64.0.7:8790/hermlet"
    assert n("https://hermes.example.ts.net:8443") == "wss://hermes.example.ts.net:8443/hermlet"
    assert n("wss://h.example/custom") == "wss://h.example/custom"
    with pytest.raises(ValueError):
        n("")
    with pytest.raises(ValueError):
        n("ftp://nope")


def test_naming_the_critter_on_one_device_changes_it_on_all(run, short_tmp):
    async def scenario():
        async with dev_hub() as (hub, brain, url):
            kitchen, study = make_device(url, short_tmp / "a", name="Kitchen"), make_device(url, short_tmp / "b", name="Study")
            heard = []
            study.on("persona")(heard.append)
            async with running(kitchen), running(study):
                await kitchen.wait_ready(10)
                await study.wait_ready(10)
                assert kitchen.persona_chosen is False
                shape = kitchen.persona["sprite"]
                result = await kitchen.set_persona("Pib", "rose")
                assert result["memory"] == "saved" and kitchen.persona["name"] == "Pib" and kitchen.persona_chosen
                assert kitchen.persona["sprite"] == shape                       # the same critter, renamed and recoloured
                await until(lambda: heard and heard[-1]["persona"]["name"] == "Pib", what="the other device to follow")
                assert study.persona["colour"] == "#ec7fa8"
                assert "named Pib, a rose" in brain.memory_note
    run(scenario())
