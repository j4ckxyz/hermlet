"""Hermlet: a Hermes platform plugin for devices built with the Hermlet SDK.

Import-light on purpose: the adapter (and the gateway modules it needs) load
only when the gateway asks for the platform.
"""

from __future__ import annotations

PLATFORM_HINT = (
    "You are talking through a hermlet: a small personal device (often a Raspberry Pi with a "
    "screen) that belongs to the user. Replies are shown as plain text in a chat bubble, so "
    "write conversationally and keep Markdown to a minimum: no tables or headings, and code "
    "blocks only when asked. The user may attach files and pictures, or speak instead of type."
)


def check_requirements() -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec("websockets.asyncio.server") is not None
    except (ImportError, ValueError):
        return False


def _make_adapter(config):
    from .adapter import HermletAdapter

    return HermletAdapter(config)


def _is_connected(config) -> bool:
    return True  # enabling the platform in config.yaml is the opt-in; no credential is needed


def _parse_target(raw: str):
    """``hermlet:<device-id>`` targets for send_message and cron delivery."""
    from .protocol import DEVICE_ID_RE

    ref = (raw or "").strip().lower()
    return (ref, None) if DEVICE_ID_RE.match(ref) else None


def register(ctx) -> None:
    from . import cli
    from .tools import register_tools

    ctx.register_platform(
        name="hermlet",
        label="Hermlet",
        adapter_factory=_make_adapter,
        check_fn=check_requirements,
        is_connected=_is_connected,
        parse_target_ref_fn=_parse_target,
        install_hint="websockets is a core Hermes dependency; reinstall Hermes if it is missing",
        allowed_users_env="HERMLET_ALLOWED_USERS",
        allow_all_env="HERMLET_ALLOW_ALL_USERS",
        max_message_length=8000,
        emoji="🐣",
        pii_safe=True,
        allow_update_command=False,
        platform_hint=PLATFORM_HINT,
    )
    register_tools(ctx)
    ctx.register_cli_command(
        name="hermlet",
        help="Manage hermlets (devices built with the Hermlet SDK)",
        setup_fn=cli.setup_argparse,
        handler_fn=cli.handle,
    )
