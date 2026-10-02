"""`recordUrl` on the approvals DTO -- one write on the row, every view reads
it (§6). No endpoint of its own: an approval is already served by the inbox,
the widget and the decision response, and all three go through
`approval_to_dto`."""

from __future__ import annotations

import uuid

from oc8 import models as m
from oc8.api.v1._serializers import approval_to_dto


def _row(**kw: object) -> m.ApprovalRequest:
    return m.ApprovalRequest(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        action_type="tool_send",
        title="Nora wants to call create_record",
        detail="over 3000 EUR",
        payload={"tool": "create_record", "arguments": {"model": "sale.order"}},
        status="pending",
        **kw,
    )


def test_a_resolved_link_reaches_the_dto() -> None:
    dto = approval_to_dto(_row(record_url="https://odoo.example.com/odoo/sale.order/42"))
    assert dto.record_url == "https://odoo.example.com/odoo/sale.order/42"


def test_a_row_without_one_serializes_as_null() -> None:
    dto = approval_to_dto(_row())
    assert dto.record_url is None


def test_the_camel_case_wire_name_is_record_url() -> None:
    dto = approval_to_dto(_row(record_url="https://x/y/1"))
    assert dto.model_dump(by_alias=True)["recordUrl"] == "https://x/y/1"


def test_a_row_that_predates_the_column_falls_back_to_the_payload_link() -> None:
    """Rows raised before `approval_request.record_url` existed only carry the
    link in their JSON payload -- the DTO must not silently drop it."""
    row = _row()
    row.payload = {**row.payload, "record_url": "https://odoo.example.com/odoo/sale.order/7"}
    assert approval_to_dto(row).record_url == "https://odoo.example.com/odoo/sale.order/7"


def test_the_column_wins_over_the_payload_link() -> None:
    row = _row(record_url="https://x/column/1")
    row.payload = {**row.payload, "record_url": "https://x/payload/1"}
    assert approval_to_dto(row).record_url == "https://x/column/1"
