"""The record link on a WhatsApp approval message (§6)."""

from __future__ import annotations

import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from oc8.channels.notice import ApprovalNotice

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _evict() -> None:
    """`channel/` is a folder name several plugins share and sys.modules is
    keyed by NAME -- mirrors test_channel_html.py's own convention."""
    for stale in [n for n in sys.modules if n == "channel" or n.startswith("channel.")]:
        del sys.modules[stale]


@pytest.fixture(autouse=True)
def _plugin_path() -> Iterator[None]:
    _evict()
    sys.path.insert(0, str(PLUGIN_ROOT))
    yield
    sys.path.remove(str(PLUGIN_ROOT))
    _evict()


def _notice(**kw: object) -> ApprovalNotice:
    return ApprovalNotice(
        approval_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        title="Nora wants to create a quotation",
        detail="over 3000 EUR",
        **kw,
    )


def test_the_link_is_in_the_message() -> None:
    from channel.channel import render

    body = render(_notice(record_url="https://odoo.example.com/odoo/sale.order/42"))
    assert "https://odoo.example.com/odoo/sale.order/42" in body


def test_no_link_means_no_extra_line() -> None:
    from channel.channel import render

    assert render(_notice()).count("\n\n") == 1  # title/detail separator only


def test_a_withheld_notice_carries_no_link() -> None:
    from channel.channel import render

    body = render(_notice(record_url="https://odoo.example.com/odoo/sale.order/42").redacted())
    assert "odoo.example.com" not in body
