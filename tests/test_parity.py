"""The Hermes plugin is installed on its own, so it carries its own copies of two small
modules. These tests keep the copies from drifting apart."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermlet import devserver, persona, protocol

REPO = Path(__file__).resolve().parents[1]


def test_the_critter_generator_is_one_file_in_two_places():
    assert (REPO / "plugin" / "persona.py").read_text() == (REPO / "src" / "hermlet" / "persona.py").read_text()


def test_the_look_module_is_one_file_in_two_places():
    assert (REPO / "plugin" / "look.py").read_text() == (REPO / "src" / "hermlet" / "look.py").read_text()


def test_both_sides_agree_on_the_protocol():
    hub = devserver.plugin("protocol")
    for name in ("PROTO", "SUBPROTOCOL", "DEFAULT_PORT", "DEFAULT_PATH", "KEY_BYTES", "MAC_CONTEXT",
                 "CHANNEL_AUDIO", "CHANNEL_FILE", "FILE_CHUNK_BYTES", "MAX_FRAME_BYTES"):
        assert getattr(hub, name) == getattr(protocol, name), name
    key = bytes(range(32))
    assert hub.device_id_for(key) == protocol.device_id_for(key) == "hl-630dcd2966c43366"
    assert hub.mac_for(key, "hl-630dcd2966c43366", "bm9uY2U=") == protocol.mac_for(key, "hl-630dcd2966c43366", "bm9uY2U=")
    assert hub.binary(2, 7, 300, b"xy") == protocol.binary(2, 7, 300, b"xy") == b"\x02\x07\x2c\x01xy"
    assert protocol.parse_binary(b"\x01\x09\x05\x00abc") == (1, 9, 5, b"abc")


def test_a_seed_always_draws_the_same_critter():
    a, b = persona.generate("install-123"), persona.generate("install-123")
    assert a == b and persona.valid(a)
    assert persona.generate("install-456") != a
    for frame in persona.FRAMES:
        rows = a["sprite"]["frames"][frame]
        assert len(rows) == 16 and all(len(r) == 16 and set(r) <= set(a["legend"]) for r in rows)
    assert persona.to_ansi(a).count("\n") == 7


def test_a_tailscale_name_resolves_without_magicdns(monkeypatch):
    """A device on the tailnet with MagicDNS off still finds Hermes by name."""
    import socket

    from hermlet import net

    status = {"Self": {"DNSName": "kitchen-pi.tail1234.ts.net.", "TailscaleIPs": ["100.64.0.9"]},
              "Peer": {"a": {"DNSName": "hermes-box.tail1234.ts.net.", "TailscaleIPs": ["100.64.0.7", "fd7a::1"]},
                       "b": {"DNSName": "", "TailscaleIPs": ["100.1.1.1"]}}}
    assert net.peers(status) == {"kitchen-pi.tail1234.ts.net": "100.64.0.9", "kitchen-pi": "100.64.0.9",
                                 "hermes-box.tail1234.ts.net": "100.64.0.7", "hermes-box": "100.64.0.7"}
    monkeypatch.setitem(net._cache, "names", net.peers(status))
    monkeypatch.setitem(net._cache, "at", 9e18)
    net.install()
    found = socket.getaddrinfo("hermes-box.tail1234.ts.net", 443, type=socket.SOCK_STREAM)
    assert found[0][4][0] == "100.64.0.7" and net.used["hermes-box.tail1234.ts.net"] == "100.64.0.7"
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("not-on-the-tailnet.invalid", 443)   # anything else still fails as before
    assert socket.getaddrinfo("localhost", 80)                  # and ordinary names are untouched
