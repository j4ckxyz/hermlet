"""The local service and the app-facing client: ``hermlet run`` + ``hermlet.connect()``."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import threading
import time

import pytest

from conftest import dev_hub, until
from hermlet import HermletError, config, connect
from hermlet.service import Service


@contextlib.asynccontextmanager
async def service_for(url, home):
    home.mkdir(parents=True, exist_ok=True)
    config.save({"url": url, "name": "Kitchen Pi", "inbox": str(home / "inbox")}, home)
    service = Service(home)
    task = asyncio.ensure_future(service.run())
    try:
        await until(lambda: config.socket_path(home).exists(), what="the local socket")
        yield service
    finally:
        await service.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def in_thread(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


def test_an_app_asks_and_gets_the_answer(run, short_tmp):
    async def scenario():
        async with dev_hub() as (hub, brain, url):
            async with service_for(url, short_tmp / "h") as service:
                await service.device.wait_ready(10)

                def app():
                    with connect(config.socket_path(short_tmp / "h")) as h:
                        assert h.status["device"]["name"] == "Kitchen Pi"
                        assert h.status["persona"]["name"]
                        return h.ask("what is for dinner?"), h.refresh()

                answer, status = await in_thread(app)
                assert answer == "Echo: what is for dinner?"
                assert [(m["role"], m["text"]) for m in status["chat"]] == [
                    ("user", "what is for dinner?"), ("assistant", "Echo: what is for dinner?")]
                assert status["state"] == "ready" and status["busy"] is False
    run(scenario())


def test_every_app_sees_what_another_sent(run, short_tmp):
    async def scenario():
        async with dev_hub() as (hub, brain, url):
            async with service_for(url, short_tmp / "h") as service:
                await service.device.wait_ready(10)
                path = config.socket_path(short_tmp / "h")

                def watcher(ready: threading.Event):
                    seen = []
                    with connect(path) as h:
                        ready.set()
                        for msg in h.events(timeout=10):
                            seen.append(msg)
                            if msg["type"] == "turn.end":
                                return seen
                    return seen

                ready = threading.Event()
                watching = asyncio.ensure_future(in_thread(watcher, ready))
                await in_thread(ready.wait, 5)
                photo = short_tmp / "cat.jpg"
                photo.write_bytes(b"\xff\xd8\xff" + b"0" * 5000)
                await in_thread(lambda: connect(path).send("look at this", files=[photo]))
                seen = await watching
                kinds = [m["type"] for m in seen]
                assert "uploading" in kinds and "user" in kinds and "reply" in kinds
                user = next(m for m in seen if m["type"] == "user")
                assert user["text"] == "look at this" and user["files"][0]["name"] == "cat.jpg"
                assert "cat.jpg (image/jpeg, 5003 bytes" in next(m for m in seen if m["type"] == "reply")["text"]
    run(scenario())


def test_an_app_offers_an_action_the_agent_can_call(run, short_tmp):
    async def scenario():
        async with dev_hub() as (hub, brain, url):
            async with service_for(url, short_tmp / "h") as service:
                await service.device.wait_ready(10)
                path = config.socket_path(short_tmp / "h")
                started, stop = threading.Event(), threading.Event()

                def app():
                    h = connect(path)

                    @h.action("lamp.set", "Turn the lamp on or off.",
                              {"type": "object", "properties": {"on": {"type": "boolean"}}})
                    def lamp(on: bool = True):
                        return {"on": on}

                    started.set()
                    threading.Thread(target=h.run_forever, daemon=True).start()
                    stop.wait(10)
                    h.close()

                running_app = asyncio.ensure_future(in_thread(app))
                await in_thread(started.wait, 5)
                session = hub.get(service.device.device_id)
                await until(lambda: any(a["name"] == "lamp.set" for a in session.actions), what="the app's action")
                assert "device.health" in [a["name"] for a in session.actions]
                assert await session.invoke("lamp.set", {"on": False}) == {"on": False}
                health = await session.invoke("device.health")
                assert "time" in health

                stop.set()
                await running_app
                # The action leaves with the app that offered it.
                await until(lambda: all(a["name"] != "lamp.set" for a in session.actions), what="the action to go")
    run(scenario())


def test_any_language_can_use_the_socket(run, short_tmp):
    """The local API is plain JSON lines; this client uses nothing from the SDK."""
    async def scenario():
        async with dev_hub() as (hub, brain, url):
            async with service_for(url, short_tmp / "h") as service:
                await service.device.wait_ready(10)

                def raw():
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.connect(str(config.socket_path(short_tmp / "h")))
                    f = s.makefile("rwb")
                    hello = json.loads(f.readline())
                    f.write(b'{"op":"send","text":"hi from C","req":"1"}\n')
                    f.write(b'{"op":"nonsense","req":"2"}\n')
                    f.flush()
                    lines = []
                    while not any(m.get("type") == "turn.end" for m in lines):
                        lines.append(json.loads(f.readline()))
                    s.close()
                    return hello, lines

                hello, lines = await in_thread(raw)
                assert hello["type"] == "hello" and hello["state"] == "ready"
                results = {m["req"]: m for m in lines if m["type"] == "result"}
                assert results["1"]["ok"] is True
                assert results["2"]["ok"] is False and "unknown op" in results["2"]["error"]
                assert any(m["type"] == "reply" and m["text"] == "Echo: hi from C" for m in lines)
    run(scenario())


def test_history_and_settings_through_the_service(run, short_tmp):
    async def scenario():
        async with dev_hub() as (hub, brain, url):
            async with service_for(url, short_tmp / "h") as service:
                await service.device.wait_ready(10)

                def app():
                    with connect(config.socket_path(short_tmp / "h")) as h:
                        sessions = h.sessions()
                        discord = h.session_messages("dev-discord")
                        resumed = h.resume("dev-discord")
                        chat = h.refresh()["chat"]
                        h.request("set", theme="light")
                        with pytest.raises(HermletError):
                            h.request("set", theme="purple")
                        assert h.set_look(accent="red", fun=False)["accent"] == "#e5484d"
                        h.new_session()
                        return sessions, discord, resumed, chat, h.refresh()

                sessions, discord, resumed, chat, after = await in_thread(app)
                assert len(sessions) == 3
                assert discord[0]["content"] == "Find me a train to Lisbon"
                assert resumed["id"] == "dev-discord"
                assert [m["text"] for m in chat] == ["Find me a train to Lisbon", "There is one at 9:40 from Porto."]
                assert after["chat"] == [] and after["theme"] == "light"
                assert after["look"]["fun"] is False and after["look"]["accent"] == "#e5484d"
    run(scenario())


def test_not_running_is_explained(short_tmp):
    with pytest.raises(HermletError, match="not running"):
        connect(short_tmp / "nope.sock")


def test_a_second_service_refuses_to_start(run, short_tmp):
    async def scenario():
        async with dev_hub() as (hub, brain, url):
            async with service_for(url, short_tmp / "h"):
                with pytest.raises(HermletError, match="already running"):
                    await Service(short_tmp / "h").start()
    run(scenario())


def test_an_app_names_and_colours_the_critter(run, short_tmp):
    async def scenario():
        async with dev_hub() as (hub, brain, url):
            async with service_for(url, short_tmp / "h") as service:
                await service.device.wait_ready(10)

                def app():
                    with connect(config.socket_path(short_tmp / "h")) as h:
                        first = dict(h.status)
                        suggestion = h.suggest_name()
                        preview = h.request("persona.preview", colour="sky")["persona"]
                        with pytest.raises(HermletError, match="not a colour"):
                            h.set_persona("Pib", "sparkly")
                        result = h.set_persona("Pib", "sky")
                        return first, suggestion, preview, result, h.refresh()

                first, suggestion, preview, result, after = await in_thread(app)
                assert first["persona_chosen"] is False
                assert [s["name"] for s in first["swatches"]][:3] == ["moss", "mint", "teal"]
                assert 2 <= len(suggestion) <= 8 and suggestion[0].isupper()
                assert preview["colour"] == "#69b7e8" and preview["chosen"] is False
                assert result["memory"] == "saved" and result["persona"]["name"] == "Pib"
                assert after["persona_chosen"] is True and after["persona"]["colour"] == "#69b7e8"
    run(scenario())


def test_coming_back_after_a_long_while_starts_a_fresh_conversation(run, short_tmp):
    """A conversation is re-sent to the model every turn, so one left running for days costs
    more and more. After the idle time in the look, the next visit starts a new one."""
    async def scenario():
        async with dev_hub() as (hub, brain, url):
            async with service_for(url, short_tmp / "h") as service:
                await service.device.wait_ready(10)
                path = config.socket_path(short_tmp / "h")
                assert service.device.look["fresh_after_min"] == 20            # the sensible default

                def say(text):
                    with connect(path) as h:
                        return h.ask(text), h.refresh()["chat"]

                await in_thread(say, "first thing")
                _, chat = await in_thread(say, "second thing")
                assert len(chat) == 4                                         # one conversation so far

                service._last_activity -= 21 * 60                             # twenty-one minutes pass
                assert await service._freshen() is True                       # what the idle watcher does
                assert service.chat == []
                _, chat = await in_thread(say, "back again")
                assert [m["text"] for m in chat] == ["back again", "Echo: back again"]

                # A visit after a long while also starts fresh by itself, even between watcher ticks.
                service._last_activity -= 21 * 60
                _, chat = await in_thread(say, "much later")
                assert [m["text"] for m in chat] == ["much later", "Echo: much later"]

                # "Never" keeps one conversation going.
                def never():
                    with connect(path) as h:
                        h.set_look(fresh_after_min=0)
                await in_thread(never)
                service._last_activity -= 5 * 24 * 3600
                assert await service._freshen() is False
                _, chat = await in_thread(say, "still here")
                assert len(chat) == 4
    run(scenario())


def test_the_app_can_do_the_whole_setup(run, short_tmp):
    """Nothing is configured: the service still starts, says so, and takes the address from an app."""
    async def scenario():
        async with dev_hub() as (hub, brain, url):
            home = short_tmp / "h"
            home.mkdir()
            service = Service(home)                       # no config.json at all
            task = asyncio.ensure_future(service.run())
            try:
                await until(lambda: config.socket_path(home).exists(), what="the local socket")
                path = config.socket_path(home)

                def app():
                    with connect(path) as h:
                        first = dict(h.refresh())  # a copy: the client keeps its snapshot up to date in place
                        with pytest.raises(HermletError, match="does not know where"):
                            h.send("too early")
                        with pytest.raises(HermletError):
                            h.request("setup", url="ftp://nope")
                        h.request("setup", timeout=40, url=url, name="Hall Pi")
                        deadline = time.time() + 10
                        while h.refresh()["state"] != "ready" and time.time() < deadline:
                            time.sleep(0.05)
                        return first, h.refresh(), h.ask("hello from a fresh device")

                first, after, answer = await in_thread(app)
                assert first["state"] == "setup" and first["mode"] == "setup" and first["url"] == ""
                assert after["state"] == "ready" and after["device"]["name"] == "Hall Pi" and after["url"] == url
                assert answer == "Echo: hello from a fresh device"
                assert config.load(home)["url"] == url          # remembered for next time

                # Moving to another Hermes: the old one's critter is not shown while the new one is found.
                async with dev_hub(seed="another") as (hub2, brain2, url2):
                    def move():
                        with connect(path) as h:
                            before = h.refresh()["persona"]["name"]
                            h.request("setup", timeout=40, url=url2)
                            deadline = time.time() + 10
                            while (h.refresh()["state"] != "ready" or h.status["url"] != url2) and time.time() < deadline:
                                time.sleep(0.05)
                            return before, h.refresh(), h.ask("still here?")
                    before, moved, answer = await in_thread(move)
                    assert moved["url"] == url2 and moved["device"]["name"] == "Hall Pi" and moved["chat"] == []  # a fresh page
                    assert moved["persona"]["name"] != before and answer == "Echo: still here?"
            finally:
                await service.stop()
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
    run(scenario())
