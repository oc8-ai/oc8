"""The mail capas declare who their sends reach (E2E N1, security ruling).

Against the REAL manifests and the REAL core function: a send is keyed by its
normalised recipients, so a reworded repeat to the same person is refused and
a mail to somebody else is not -- in every capa that sends mail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from oc8.agent.outward import outward_target
from oc8.capas.discovery import find_plugin
from oc8.capas.manifest import parse_manifest

_CAPAS = Path(__file__).resolve().parents[3] / "capas"


def _config(plugin: str) -> dict[str, Any]:
    discovered = find_plugin(plugin, [str(_CAPAS)])
    assert discovered is not None and discovered.valid, plugin
    assert discovered.manifest is not None
    manifest = parse_manifest(discovered.manifest)
    assert manifest.tool_pack is not None
    return dict(manifest.tool_pack.connections[0].config)


def _target(plugin: str, tool: str, arguments: dict[str, Any]) -> str | None:
    cfg = _config(plugin)
    return outward_target(tool, arguments, cfg.get("focus_spec"), cfg.get("outward_tools"))


@pytest.mark.parametrize(
    ("plugin", "send", "to"),
    [
        ("email", "mail_send", ["bob@example.com"]),
        ("microsoft365", "mail_send", ["bob@example.com"]),
        ("google_workspace", "gmail_send", ["bob@example.com"]),
    ],
)
def test_a_send_is_keyed_by_its_recipients(plugin: str, send: str, to: list[str]) -> None:
    first = _target(plugin, send, {"to": to, "subject": "Offer", "body": "x"})
    assert first is not None
    reworded = _target(plugin, send, {"to": ["BOB@example.com"], "subject": "Re", "body": "y"})
    other = _target(plugin, send, {"to": ["ann@example.com"], "subject": "Offer", "body": "x"})
    assert reworded == first
    assert other != first


def test_an_email_forward_reaches_its_recipients_not_the_mail() -> None:
    sent = _target("email", "mail_send", {"to": ["bob@example.com"], "subject": "s", "body": "b"})
    forwarded = _target("email", "mail_forward", {"id": "7", "to": ["bob@example.com"]})
    elsewhere = _target("email", "mail_forward", {"id": "7", "to": ["ann@example.com"]})
    assert forwarded == sent
    assert elsewhere != forwarded
