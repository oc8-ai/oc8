"""The record link on a Teams approval card, as an Action.OpenUrl (§6)."""

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


def test_the_link_is_an_open_url_action() -> None:
    from channel.channel import render_actions

    actions = render_actions(_notice(record_url="https://odoo.example.com/odoo/sale.order/42"))
    open_url = [a for a in actions if a["type"] == "Action.OpenUrl"]
    assert len(open_url) == 1
    assert open_url[0]["url"] == "https://odoo.example.com/odoo/sale.order/42"


def test_no_link_means_no_open_url_action() -> None:
    from channel.channel import render_actions

    actions = render_actions(_notice())
    assert not [a for a in actions if a["type"] == "Action.OpenUrl"]


def test_a_withheld_notice_gets_no_actions_at_all() -> None:
    from channel.channel import render_actions

    notice = _notice(record_url="https://odoo.example.com/odoo/sale.order/42").redacted()
    assert render_actions(notice) == []
