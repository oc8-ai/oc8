"""KPI response DTOs -- see backend/src/oc8/kpis/aggregate.py for the numbers
these wrap."""

from __future__ import annotations

from oc8.schemas.base import CamelModel


class KPIDTO(CamelModel):
    run_count: int
    total_duration_ms: int | None
    execution_duration_ms: int | None
    approval_wait_ms: int | None
    response_time_ms: int | None
    avg_tool_call_duration_ms: int | None


class KPIGroupRowDTO(KPIDTO):
    group_key: str


class KPIGroupedResponseDTO(CamelModel):
    rows: list[KPIGroupRowDTO]


# GET /agents/{id}/kpis and GET /departments/{id}/kpis return a bare
# KPIDTO (single scope, never grouped). GET /kpis returns KPIDTO when
# group_by is omitted, KPIGroupedResponseDTO when it is set -- FastAPI's
# response_model can't express a conditional shape cleanly, so that
# endpoint's response_model is left unset and it returns a plain dict
# built from whichever DTO applies.


class FeedbackCountDTO(CamelModel):
    """Answer feedback (thumbs) for one agent and model. `model` is None for
    a rating written before its event named the model. `is_assistant` marks
    the tenant Assistant (Copilot), whose stored name is not what people
    call it."""

    agent_id: str
    agent_name: str
    is_assistant: bool
    model: str | None
    up: int
    down: int
