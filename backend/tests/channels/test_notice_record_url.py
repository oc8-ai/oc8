"""The record link travels to every channel through the neutral notice (§6),
and is REMOVED when the notice is redacted.

`https://.../sale.order/42` names the system, the entity and the record. A
notice redacted because its material outranks the channel must not carry it --
the same reasoning `keyboard()` gives for withholding the buttons.
"""

from __future__ import annotations

import uuid

from oc8 import models as m
from oc8.channels.dispatch import notice_for


def _row(**kw: object) -> m.ApprovalRequest:
    return m.ApprovalRequest(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        action_type="tool_send",
        title="Nora wants to create a quotation",
        detail="over 3000 EUR",
        amount_text="4.200,00 €",
        payload={},
        status="pending",
        **kw,
    )


def test_the_notice_carries_the_link() -> None:
    notice = notice_for(_row(record_url="https://odoo.example.com/odoo/sale.order/42"))
    assert notice.record_url == "https://odoo.example.com/odoo/sale.order/42"


def test_a_row_without_one_gives_an_empty_string_not_none() -> None:
    assert notice_for(_row()).record_url == ""


def test_redacting_removes_the_link() -> None:
    notice = notice_for(_row(record_url="https://odoo.example.com/odoo/sale.order/42"))
    hidden = notice.redacted()
    assert hidden.record_url == ""
    assert hidden.content_withheld is True
