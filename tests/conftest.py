"""Shared fixtures. Everything here runs with no Hermes, no hardware and no network
beyond 127.0.0.1: the real hub (the plugin's own code) with the dev server's echo brain."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))


@pytest.fixture
def run():
    """Run a coroutine to completion on a fresh loop (keeps the suite free of plugins)."""
    def _run(coro, timeout: float = 30):
        async def bounded():
            return await asyncio.wait_for(coro, timeout)
        return asyncio.run(bounded())
    return _run


@pytest.fixture
def short_tmp():
    """A short temporary directory: Unix socket paths are limited to about 100 characters."""
    path = tempfile.mkdtemp(prefix="hl-", dir="/tmp")
    try:
        yield Path(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)  # a gateway under test may still be writing as it stops


@contextlib.asynccontextmanager
async def dev_hub(**kw):
    from hermlet import devserver

    kw.setdefault("word_delay", 0.0)
    hub, brain = await devserver.serve(**kw)
    try:
        yield hub, brain, f"ws://127.0.0.1:{hub.bound_port}/hermlet"
    finally:
        await hub.stop()


@contextlib.asynccontextmanager
async def running(device):
    """Run a Device in the background for the duration of the block."""
    task = asyncio.ensure_future(device.run())
    try:
        yield device
    finally:
        await device.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def until(predicate, timeout: float = 10, what: str = "condition"):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


os.environ.setdefault("HERMLET_HOME", tempfile.mkdtemp(prefix="hl-default-", dir="/tmp"))
