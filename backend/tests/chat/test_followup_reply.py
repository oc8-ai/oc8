from __future__ import annotations

import uuid

from oc8 import models as m
from oc8.copilot.followups import is_quiet_followup


def _run(ctx: dict[str, object]) -> m.AgentRun:
    return m.AgentRun(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        source="chat",
        state="done",
        context=ctx,
    )


def test_ordinary_chat_run_is_never_quiet() -> None:
    assert is_quiet_followup(_run({"chat_session_id": "s"}), None) is False


def test_followup_without_report_is_quiet() -> None:
    run = _run({"followup": {"responsibility_id": "r"}})
    r = m.Responsibility(last_report_run_id=None)
    assert is_quiet_followup(run, r) is True


def test_followup_that_reported_is_not_quiet() -> None:
    run = _run({"followup": {"responsibility_id": "r"}})
    r = m.Responsibility(last_report_run_id=run.id)
    assert is_quiet_followup(run, r) is False
