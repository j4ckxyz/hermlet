"""The default route: a hermlet using Hermes's own web API, with nothing installed on Hermes."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import struct

import pytest

from conftest import running, until
from fakes.dashboard import FakeDashboard
from hermlet import DashboardDevice, Device, HermletError, HubDevice, mode_for


class _Device(DashboardDevice):
    """Points the WebSocket at the fake's second port; everything else is the real class."""
    fake: FakeDashboard

    def _ws_url(self, query: str) -> str:
        return f"ws://127.0.0.1:{self.fake.ws_port}/api/ws?{query}"


@contextlib.asynccontextmanager
async def dashboard(**kw):
    fake = FakeDashboard(**kw)
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


def make(fake: FakeDashboard, home, name="Kitchen Pi") -> _Device:
    device = _Device(fake.base, name, home=home)
    device.fake = fake
    return device


async def signed_in(fake, home):
    device = make(fake, home)
    await device.sign_in("ada", "pw")
    return device


def test_the_address_decides_the_route():
    assert mode_for("https://hermes.tail1234.ts.net") == "dashboard"
    assert mode_for("hermes-box:9119") == "dashboard"
    assert mode_for("ws://hermes-box:8790/hermlet") == "plugin"
    assert mode_for("https://hermes.example/hermlet") == "plugin"
    assert mode_for("ws://x", "dashboard") == "dashboard"


def test_device_picks_the_backend(short_tmp):
    assert isinstance(Device("https://hermes.example", home=short_tmp / "a"), DashboardDevice)
    assert isinstance(Device("ws://hermes.example:8790/hermlet", home=short_tmp / "b"), HubDevice)


def test_sign_in_keeps_a_session_not_the_password(run, short_tmp):
    async def scenario():
        async with dashboard() as fake:
            device = make(fake, short_tmp / "h")
            states = []
            device.on("link")(lambda m: states.append(m["state"]))
            async with running(device):
                await until(lambda: device.state == "signin", what="being asked to sign in")
                with pytest.raises(HermletError, match="not right"):
                    await device.sign_in("ada", "wrong")
                await device.sign_in("ada", "pw")
                await device.wait_ready(10)
                assert await device.ask("hello") == "Echo: hello"
            stored = (short_tmp / "h" / "auth.json").read_text()
            assert "pw" not in json.loads(stored)["cookies"].values() and '"pw"' not in stored
            assert oct((short_tmp / "h" / "auth.json").stat().st_mode & 0o777) == "0o600"
            assert states[:2] == ["connecting", "signin"] and "ready" in states

            # A restart needs no password, and follows the cookie Hermes rotated meanwhile.
            again = make(fake, short_tmp / "h")
            async with running(again):
                await again.wait_ready(10)
                assert again.http.cookies["hermes_session_at"] == fake.access
    run(scenario())


def test_an_expired_session_asks_to_sign_in_again(run, short_tmp):
    async def scenario():
        async with dashboard() as fake:
            device = await signed_in(fake, short_tmp / "h")
            async with running(device):
                await device.wait_ready(10)
            fake.expire_session()
            again = make(fake, short_tmp / "h")
            errors = []
            again.on("link")(lambda m: errors.append((m["state"], m.get("error"))))
            async with running(again):
                await until(lambda: again.state == "signin", what="the sign-in state")
                assert any(e and "sign in" in e.lower() for _, e in errors)
                await again.sign_in("ada", "pw")   # wakes the device at once, no 60 s wait
                await again.wait_ready(10)
    run(scenario())


def test_an_open_local_dashboard_needs_no_sign_in(run, short_tmp):
    async def scenario():
        async with dashboard(auth_required=False) as fake:
            async with running(make(fake, short_tmp / "h")) as device:
                await device.wait_ready(10)
                assert await device.ask("hi") == "Echo: hi"
    run(scenario())


def test_a_turn_streams_in_order_and_ignores_other_conversations(run, short_tmp):
    async def scenario():
        async with dashboard() as fake:
            device = await signed_in(fake, short_tmp / "h")
            seen = []
            device.on("*")(seen.append)
            async with running(device):
                await device.wait_ready(10)
                assert await device.ask("search the weather") == "Echo: search the weather"
            kinds = [m["type"] for m in seen]
            assert kinds.index("turn.start") < kinds.index("tool") < kinds.index("reply.delta") < kinds.index("reply") < kinds.index("turn.end")
            deltas = [m["text"] for m in seen if m["type"] == "reply.delta"]
            assert deltas == sorted(deltas, key=len) and deltas[-1].strip() == "Echo: search the weather"
            assert not any("SOMEONE ELSE" in json.dumps(m) for m in seen)
            assert {"type": "status", "text": "Web search: weather lisbon"} in seen
            assert next(m for m in seen if m["type"] == "session")["title"] == "search the weather"
            # The conversation is an ordinary Hermes session tagged as coming from a hermlet.
            assert fake.sessions[device.stored_session]["source"] == "hermlet"
    run(scenario())


def test_a_failed_turn_ends_and_says_why(run, short_tmp):
    async def scenario():
        async with dashboard() as fake:
            device = await signed_in(fake, short_tmp / "h")
            seen = []
            device.on("*")(seen.append)
            async with running(device):
                await device.wait_ready(10)
                assert await device.ask("please fail") == ""
            assert {"type": "turn.end", "outcome": "failure"} in seen
            assert any(m["type"] == "notice" and "unavailable" in m["text"] for m in seen)
    run(scenario())


def test_images_and_files_go_the_right_way(run, short_tmp):
    async def scenario():
        async with dashboard() as fake:
            device = await signed_in(fake, short_tmp / "h")
            photo = short_tmp / "cat.PNG"
            photo.write_bytes(b"\x89PNG" + b"0" * 2000)
            async with running(device):
                await device.wait_ready(10)
                reply = await device.ask("look", [photo, ("notes.txt", b"milk")])
                assert reply == "Echo: @file:notes.txt look (+1 image(s): cat.PNG, 2004 bytes)"
                attach = next(p for m, p in fake.rpc_log if m == "file.attach")
                assert attach["data_url"].startswith("data:text/plain;base64,") and attach["name"] == "notes.txt"
                with pytest.raises(HermletError, match="too large"):
                    await device.send("big", [("big.bin", b"x" * (11 * 1024 * 1024))])
    run(scenario())


def test_approvals_and_questions_from_the_agent(run, short_tmp):
    async def scenario():
        async with dashboard() as fake:
            device = await signed_in(fake, short_tmp / "h")
            prompts = []

            async def answer(msg):
                prompts.append(msg)
                reply = {"Allow this command?": "yes", "Tea or coffee?": "coffee", "Any sugar?": "one spoon"}
                await device.answer(msg["id"], reply.get(msg["title"], reply.get(msg["text"])))

            device.on("prompt")(answer)
            async with running(device):
                await device.wait_ready(10)
                assert await device.ask("do the dangerous thing") == "approval: once"
                assert prompts[0]["text"] == "rm -rf build\ndelete build output"
                assert prompts[0]["choices"] == ["once", "session", "always", "deny"]
                assert await device.ask("which drink") == 'clarified: {"q1": "coffee", "q2": "one spoon"}'
                assert [p.get("free_text") for p in prompts[1:]] == [False, True]
                with pytest.raises(HermletError, match="expired"):
                    await device.answer("srq-gone", "yes")
    run(scenario())


def test_history_from_every_surface(run, short_tmp):
    async def scenario():
        async with dashboard() as fake:
            device = await signed_in(fake, short_tmp / "h")
            async with running(device):
                await device.wait_ready(10)
                await device.ask("remember the milk")
                rows = await device.sessions()
                assert [r["source"] for r in rows] == ["hermlet", "discord"]       # automation noise is left out
                assert rows[0]["current"] is True and rows[1]["current"] is False
                assert [r["id"] for r in await device.sessions(source="cron")] == ["cron-1"]   # unless asked for
                assert [r["id"] for r in await device.sessions(query="train")] == ["disc-1"]
                discord = await device.session_messages("disc-1")
                assert [(m["role"], m["content"]) for m in discord["messages"]] == [
                    ("user", "Find me a train"), ("assistant", "There is one at 9:40.")]   # tool rows are not chat
                mine = await device.session_messages(device.stored_session)
                assert mine["messages"][0]["content"] == "remember the milk"   # without the inlined file text
                with pytest.raises(HermletError, match="no such conversation"):
                    await device.session_messages("nope")
    run(scenario())


def test_continuing_a_discord_conversation_leaves_the_original_alone(run, short_tmp):
    async def scenario():
        async with dashboard() as fake:
            device = await signed_in(fake, short_tmp / "h")
            async with running(device):
                await device.wait_ready(10)
                before = json.dumps(fake.messages["disc-1"])
                resumed = await device.resume("disc-1")
                new_id = resumed["session"]["id"]
                assert new_id != "disc-1" and resumed["session"]["continued_from"] == "disc-1"
                assert fake.sessions[new_id]["source"] == "hermlet" and fake.sessions[new_id]["parent_session_id"] == "disc-1"
                assert [m["content"] for m in fake.messages[new_id]][2:] == ["Find me a train", "There is one at 9:40."]
                await device.ask("and the return?")
                assert json.dumps(fake.messages["disc-1"]) == before
                assert len(fake.messages[new_id]) == 6

                # Every conversation opens with a hidden note telling the agent it is on a hermlet and
                # which file "change the look" means; the person never sees it.
                hidden = fake.messages[new_id][0]
                assert hidden["display_kind"] == "hidden" and "Kitchen Pi" in hidden["content"]
                assert "Never change Hermes's own skin" in hidden["content"] and "/plugin-data/hermlet/look.json" in hidden["content"]
                shown = (await device.session_messages(new_id))["messages"]
                assert [m["content"] for m in shown][:2] == ["Find me a train", "There is one at 9:40."]
                assert not any("Hermlet device context" in m["content"] or m["content"] == "Understood." for m in shown)

                # One of the device's own conversations is reopened as it is.
                await device.new_session()
                assert device.stored_session == ""
                again = await device.resume(new_id)
                assert again["session"]["id"] == new_id and device.stored_session == new_id
    run(scenario())


def test_the_conversation_survives_a_restart(run, short_tmp):
    async def scenario():
        async with dashboard() as fake:
            device = await signed_in(fake, short_tmp / "h")
            async with running(device):
                await device.wait_ready(10)
                await device.ask("first")
                stored = device.stored_session
            again = make(fake, short_tmp / "h")
            async with running(again):
                await again.wait_ready(10)
                assert again.stored_session == stored
                await again.ask("second")
                assert [m["content"].split("\n")[0] for m in fake.messages[stored]
                        if m["role"] == "user" and m.get("display_kind") != "hidden"] == ["first", "second"]
            # If Hermes no longer has that conversation, the device quietly starts a new one.
            del fake.sessions[stored]
            third = make(fake, short_tmp / "h")
            async with running(third):
                await third.wait_ready(10)
                assert third.stored_session == ""
                await third.ask("third")
                assert third.stored_session not in ("", stored)
    run(scenario())


def test_voice_is_transcribed_by_hermes_then_sent_as_text(run, short_tmp):
    async def scenario():
        pcm = b"".join(struct.pack("<h", int(8000 * math.sin(i / 8))) for i in range(16000))
        async with dashboard() as fake:
            device = await signed_in(fake, short_tmp / "h")
            seen = []
            device.on("*")(seen.append)
            async with running(device):
                await device.wait_ready(10)
                done = asyncio.Event()
                device.on("turn.end")(lambda m: done.set())
                async with device.voice(16000) as v:
                    await v.write(pcm)
                await asyncio.wait_for(done.wait(), 10)
                assert {"type": "transcript", "text": "what is the weather"} in seen
                assert any(m["type"] == "reply" and m["text"] == "Echo: what is the weather" for m in seen)

                # No speech-to-text set up on Hermes: say so plainly, send nothing.
                fake.stt_error = "No STT provider available."
                submits = sum(1 for m, _ in fake.rpc_log if m == "prompt.submit")
                async with device.voice(16000) as v:
                    await v.write(pcm)
                assert any(m["type"] == "notice" and "No STT provider" in m["text"] for m in seen)
                assert sum(1 for m, _ in fake.rpc_log if m == "prompt.submit") == submits
    run(scenario())


def test_every_device_of_one_hermes_gets_the_same_critter(run, short_tmp):
    async def scenario():
        async with dashboard() as fake:
            names = []
            for home in ("a", "b"):
                device = await signed_in(fake, short_tmp / home)
                async with running(device):
                    await device.wait_ready(10)
                    names.append(json.dumps(device.persona, sort_keys=True))
            assert names[0] == names[1]
            fake.install_id = "a-different-hermes"
            other = await signed_in(fake, short_tmp / "c")
            async with running(other):
                await other.wait_ready(10)
                assert json.dumps(other.persona, sort_keys=True) != names[0]
    run(scenario())


def test_naming_and_colouring_the_critter_reaches_every_device_and_the_agent(run, short_tmp):
    async def scenario():
        async with dashboard() as fake:
            memory = fake.home + "/memories/MEMORY.md"
            stored = fake.home + "/plugin-data/hermlet/persona.json"
            first = await signed_in(fake, short_tmp / "a")
            async with running(first):
                await first.wait_ready(10)
                assert first.persona_chosen is False            # nobody has named it yet: a device should ask
                before = first.persona
                result = await first.set_persona("Pib", "teal")
                assert result["memory"] == "saved"
                assert (first.persona["name"], first.persona["colour"], first.persona_chosen) == ("Pib", "#4fb3a6", True)
                assert first.persona["sprite"] == before["sprite"] and first.persona["species"] == before["species"]
                assert json.loads(fake.files[stored])["name"] == "Pib"     # kept on Hermes, not on the device
                notes = fake.files[memory].split("\n\u00a7\n")
                assert notes[:2] == ["User likes short answers", "The server runs Ubuntu"]   # the agent's notes are kept
                assert "named Pib, a teal" in notes[2] and len(notes) == 3

            # A second device of the same Hermes shows the same critter without being told.
            second = await signed_in(fake, short_tmp / "b")
            seen = []
            second.on("persona")(seen.append)
            async with running(second):
                await second.wait_ready(10)
                assert (second.persona["name"], second.persona["colour"], second.persona_chosen) == ("Pib", "#4fb3a6", True)
                # Renaming replaces the agent's note rather than piling up another.
                await second.set_persona("Momo")
                assert second.persona["colour"] == "#4fb3a6"
                notes = fake.files[memory].split("\n\u00a7\n")
                assert len(notes) == 3 and "named Momo" in notes[2] and "Pib" not in fake.files[memory]
                assert seen[-1]["persona"]["name"] == "Momo"
    run(scenario())


def test_the_critter_is_saved_even_when_the_agent_cannot_be_told(run, short_tmp):
    async def scenario():
        async with dashboard() as fake:
            memory = fake.home + "/memories/MEMORY.md"
            original = fake.files[memory]
            device = await signed_in(fake, short_tmp / "a")
            async with running(device):
                await device.wait_ready(10)
                fake.memory_settings = {"memory_enabled": True, "memory_char_limit": 60}   # no room for a new note
                assert (await device.set_persona("Pib", "rose"))["memory"] == "full"
                assert fake.files[memory] == original and device.persona["name"] == "Pib"
                fake.memory_settings = {"memory_enabled": False}
                assert (await device.set_persona("Pib", "#123456"))["memory"] == "off"
                assert fake.files[memory] == original and device.persona["colour"] == "#123456"
                assert device.draw_persona(colour="moss")["colour"] == "#9ccb5a"   # a preview saves nothing
                assert device.persona["colour"] == "#123456"
    run(scenario())


def test_the_look_is_shared_and_the_agent_can_change_it_mid_conversation(run, short_tmp, monkeypatch):
    from hermlet import dashboard as dashboard_module, look as look_mod

    monkeypatch.setattr(dashboard_module, "SHARED_POLL_S", 0.2)

    async def scenario():
        async with dashboard() as fake:
            kitchen, study = await signed_in(fake, short_tmp / "a"), make(fake, short_tmp / "b", name="Study")
            await study.sign_in("ada", "pw")
            looks = []
            study.on("look")(lambda m: looks.append(m["look"]))
            async with running(kitchen), running(study):
                await kitchen.wait_ready(10)
                await study.wait_ready(10)
                assert kitchen.look == look_mod.DEFAULTS

                # A setting changed on one device reaches the other within the poll.
                shown = await kitchen.set_look({"theme": "dark", "text_size": "large", "nonsense": 1})
                assert shown["theme"] == "dark" and shown["text_size"] == "large" and "nonsense" not in shown
                await until(lambda: study.look["theme"] == "dark", what="the other device to follow")

                # One device can differ from the rest.
                await study.set_look({"text_size": "small"}, this_device_only=True)
                assert study.look["text_size"] == "small"
                await until(lambda: kitchen.look_file.get("devices"), what="the override to be stored")
                assert kitchen.look["text_size"] == "large"

                # The agent learns where the file is from a skill written through Hermes's own file API...
                assert await kitchen.teach_agent() is True
                skill = fake.files[fake.home + "/skills/hermlet/SKILL.md"]
                assert skill.startswith("---\nname: hermlet\n") and fake.home + "/plugin-data/hermlet/look.json" in skill

                # ...and when it edits the file during a turn, the device restyles before the reply ends.
                order = []
                kitchen.on("look")(lambda m: order.append(("look", m["look"]["accent"], m["look"]["font"])))
                kitchen.on("turn.end")(lambda m: order.append(("end",)))
                await kitchen.ask("restyle it: red and code-style please")
                await until(lambda: kitchen.look["accent"] == "#e5484d", what="the agent's change")
                assert kitchen.look["font"] == "mono" and kitchen.look["theme"] == "dark"
                assert ("look", "#e5484d", "mono") in order

            # The look is remembered across a restart, before Hermes has even answered.
            again = make(fake, short_tmp / "a")
            assert again.look["accent"] == "#e5484d"
    run(scenario())


def test_look_files_are_cleaned_never_trusted():
    from hermlet import look as look_mod

    look = look_mod.parse('{"theme": "DARK", "accent": "#f00", "font": "Code", "text_size": 99, "fun": "no",'
                          ' "greeting": "  hi   there ", "evil": "rm -rf", "devices": {"Pi": {"text_size": 22, "x": 1}}}')
    assert look == {"theme": "dark", "accent": "#ff0000", "font": "mono", "greeting": "hi there",
                    "devices": {"Pi": {"text_size": 22}}, "version": 1}
    assert look_mod.resolve(look, "Pi")["text_size"] == 22 and look_mod.resolve(look, "Other")["text_size"] == "medium"
    assert look_mod.parse("not json") == {"version": 1} and look_mod.parse("") == {"version": 1}
    assert look_mod.colour("Red") == "#e5484d" and look_mod.colour("#12") is None
    assert look_mod.merged(look, {"accent": None})["accent"] is None            # null clears an override
    assert look_mod.merged({}, {"font": "/usr/share/fonts/x.ttf"})["font"] == "/usr/share/fonts/x.ttf"
    assert "font" not in look_mod.merged({}, {"font": "../../etc/passwd"})
