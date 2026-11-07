"""`assigneeMemberId` / `previousApprovalId` on the approvals DTO (D3a): the
chat card needs to know which pending approval is the member's own, and
which department stage follows the member's approval."""

from __future__ import annotations

import uuid

from oc8 import models as m
from oc8.api.v1._serializers import approval_to_dto


def _row(**kw: object) -> m.ApprovalRequest:
    return m.ApprovalRequest(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        action_type="tool_send",
        title="Nora wants to send a mail",
        detail="",
        payload={"tool": "send_mail", "arguments": {}},
        status="pending",
        **kw,
    )


def test_the_routing_fields_reach_the_wire_in_camel_case() -> None:
    member, previous = uuid.uuid4(), uuid.uuid4()
    wire = approval_to_dto(
        _row(assignee_member_id=member, previous_approval_id=previous)
    ).model_dump(by_alias=True)
    assert wire["assigneeMemberId"] == str(member)
    assert wire["previousApprovalId"] == str(previous)


def test_a_department_approval_carries_neither() -> None:
    wire = approval_to_dto(_row()).model_dump(by_alias=True)
    assert wire["assigneeMemberId"] is None
    assert wire["previousApprovalId"] is None
