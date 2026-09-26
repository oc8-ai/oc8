"""Map ORM rows to the frontend-facing DTOs (src/lib/*.ts shapes)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from oc8 import models as m
from oc8.knowledge.chunks import ChunkRow, DocumentSummary
from oc8.knowledge.retrieval import SimilarChunkRow
from oc8.modelrouter.registry import canonical_provider
from oc8.schemas.dto import (
    ActivityDTO,
    AgentDTO,
    ApprovalDTO,
    ApprovalOptionDTO,
    ClarificationDTO,
    DataSourceDTO,
    DepartmentDTO,
    IngestionJobDTO,
    IntegrationDTO,
    KbChunkDTO,
    KnowledgeBaseDTO,
    KnowledgeDocumentDTO,
    MemberDTO,
    ModelDTO,
    SeatDTO,
    SimilarChunkDTO,
    SkillDTO,
    TaskBoardRowDTO,
    TaskDTO,
)
from oc8.workspace import tasks as _workspace_tasks

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

    from oc8.workspace.members import MemberRow, Seat
    from oc8.workspace.queue import ClarificationRow
    from oc8.workspace.tasks import TaskRow

# Backend lifecycle status -> the five UI states the office view renders.
# "idle" is enabled-but-not-executing (distinct from "running", which is
# reserved for a run actually in flight) -- see agent.status writes in
# oc8/runtime/executor.py, which only sets "running" once execution begins.
AGENT_STATUS_TO_UI = {
    "running": "running",
    "idle": "waiting_for_task",
    "waiting_for_approval": "warning",
    "pending_approval": "warning",
    "paused": "paused",
    "stopped": "paused",
    "error": "error",
}
#: Re-exported from `workspace.tasks`, which owns it -- see that module for why.
TASK_STATE_TO_COLUMN = _workspace_tasks.TASK_STATE_TO_COLUMN


def _i18n_str(i18n: Any, field: str) -> dict[str, str]:
    """Pull `{locale: str}` for one field out of `{de: {field: …}}`."""
    if not isinstance(i18n, dict):
        return {}
    out: dict[str, str] = {}
    for locale, block in i18n.items():
        if isinstance(block, dict):
            value = block.get(field)
            if isinstance(value, str) and value:
                out[str(locale)] = value
    return out


def _i18n_str_list(i18n: Any, field: str) -> dict[str, list[str]]:
    """Pull `{locale: [str, …]}` for one field out of `{de: {field: […]}}`."""
    if not isinstance(i18n, dict):
        return {}
    out: dict[str, list[str]] = {}
    for locale, block in i18n.items():
        if isinstance(block, dict):
            value = block.get(field)
            if isinstance(value, list) and all(isinstance(v, str) for v in value):
                out[str(locale)] = list(value)
    return out


def agent_to_dto(a: m.Agent) -> AgentDTO:
    p: dict[str, Any] = a.presentation or {}
    i18n = p.get("i18n")
    return AgentDTO(
        id=str(a.id),
        name=a.name,
        role=a.role_title,
        llm=p.get("llm", ""),
        provider=p.get("provider", ""),
        status=AGENT_STATUS_TO_UI.get(a.status, "running"),
        tools=p.get("tools", []),
        last_action=p.get("last_action", ""),
        last_run=p.get("last_run", ""),
        tasks_today=p.get("tasks_today", 0),
        guardrails=p.get("guardrails", []),
        schedule=p.get("schedule", ""),
        avatar_color=p.get("avatar_color", ""),
        department_id=str(a.department_id) if a.department_id else None,
        model_config_id=str(a.model_config_id) if a.model_config_id else None,
        is_lead=a.is_team_lead,
        deleted_at=a.deleted_at.isoformat() if a.deleted_at else None,
        role_translations=_i18n_str(i18n, "role"),
        last_action_translations=_i18n_str(i18n, "last_action"),
        last_run_translations=_i18n_str(i18n, "last_run"),
        schedule_translations=_i18n_str(i18n, "schedule"),
        mission_translations=_i18n_str(i18n, "mission"),
        guardrails_translations=_i18n_str_list(i18n, "guardrails"),
    )


def department_to_dto(d: m.Department) -> DepartmentDTO:
    p: dict[str, Any] = d.presentation or {}
    i18n = p.get("i18n")
    return DepartmentDTO(
        id=str(d.id),
        name=d.name,
        icon=p.get("icon", "support"),
        goal=d.goal or "",
        okr=p.get("okr", ""),
        kpi_label=p.get("kpi_label", ""),
        kpi_value=p.get("kpi_value", ""),
        activity=p.get("activity", 0),
        accent=p.get("accent", ""),
        prompt_caching_enabled=d.prompt_caching_enabled,
        deleted_at=d.deleted_at.isoformat() if d.deleted_at else None,
        name_translations=_i18n_str(i18n, "name"),
        goal_translations=_i18n_str(i18n, "goal"),
        okr_translations=_i18n_str(i18n, "okr"),
        kpi_label_translations=_i18n_str(i18n, "kpi_label"),
    )


def task_to_dto(t: m.Task) -> TaskDTO:
    payload: dict[str, Any] = t.payload or {}
    i18n = payload.get("i18n")
    return TaskDTO(
        id=str(t.id),
        department_id=str(t.department_id),
        title=t.title,
        agent_id=str(t.assigned_agent_id) if t.assigned_agent_id else None,
        column=TASK_STATE_TO_COLUMN.get(t.state, "backlog"),
        meta=t.meta_label,
        title_translations=_i18n_str(i18n, "title"),
        meta_translations=_i18n_str(i18n, "meta"),
    )


def activity_to_dto(e: m.ActivityEvent) -> ActivityDTO:
    i18n = e.i18n or {}
    return ActivityDTO(
        id=str(e.id),
        agent_id=str(e.agent_id) if e.agent_id else None,
        status=e.status,
        message=e.message,
        time=e.ts.strftime("%H:%M"),
        detail=e.detail,
        cache_hit=e.cache_hit,
        message_translations=_i18n_str(i18n, "message"),
        detail_translations=_i18n_str(i18n, "detail"),
    )


@dataclass(frozen=True)
class ApprovalNames:
    """The names an approvals list needs, resolved once for the whole page.

    A DTO carrying only ids makes the screen resolve four things per row, which
    is how a queue of a hundred approvals becomes four hundred and one requests.
    Empty by default so `approval_to_dto(row)` keeps working for the single-row
    doors (a decision response), where the caller already knows what it just
    acted on.
    """

    departments: dict[uuid.UUID, str] = field(default_factory=dict)
    agents: dict[uuid.UUID, str] = field(default_factory=dict)
    tasks: dict[uuid.UUID, str] = field(default_factory=dict)
    members: dict[uuid.UUID, str] = field(default_factory=dict)


_NO_NAMES = ApprovalNames()


async def resolve_approval_names(
    db: AsyncSession, rows: Sequence[m.ApprovalRequest]
) -> ApprovalNames:
    """Four batched lookups, never one per row.

    Deliberately NOT joined onto the approvals query itself: that query lives in
    `approvals/repo.py`, which is the security boundary, and widening it into a
    four-way join to decorate a screen is how a `WHERE` clause acquires a shape
    nobody can read. This runs afterwards, over rows the scope has already
    admitted, so it can see nothing the caller could not.
    """
    if not rows:
        return _NO_NAMES

    department_ids = {a.department_id for a in rows if a.department_id is not None}
    agent_ids = {a.agent_id for a in rows}
    task_ids = {a.task_id for a in rows if a.task_id is not None}
    member_ids = {a.decided_by for a in rows if a.decided_by is not None}

    async def _names(column: Any, label: Any, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not ids:
            return {}
        found = (await db.execute(select(column, label).where(column.in_(ids)))).all()
        return {row[0]: row[1] or "" for row in found}

    return ApprovalNames(
        departments=await _names(m.Department.id, m.Department.name, department_ids),
        agents=await _names(m.Agent.id, m.Agent.name, agent_ids),
        tasks=await _names(m.Task.id, m.Task.title, task_ids),
        # The person who decided it, by the name an administrator gave them --
        # falling back to nothing rather than to the raw subject, because a
        # raw uuid printed under "entschieden von" is worse than a blank.
        members=await _names(m.OrgMember.id, m.OrgMember.display_name, member_ids),
    )


def approval_to_dto(a: m.ApprovalRequest, names: ApprovalNames | None = None) -> ApprovalDTO:
    payload: dict[str, Any] = a.payload or {}
    raw_options = payload.get("options") or []
    resolved = names or _NO_NAMES
    # Two keys out of `payload`, and never `payload` itself: `_announce` writes
    # `channel_handles` into it (which messenger accounts were notified), and the
    # raisers write whatever else they please. A DTO that serialized the whole
    # blob would put all of it on a screen the moment anybody added a field.
    raw_arguments = payload.get("arguments")
    tool_name = payload.get("tool")
    return ApprovalDTO(
        id=str(a.id),
        agent_id=str(a.agent_id),
        title=a.title,
        detail=a.detail,
        amount=a.amount_text,
        time=payload.get("time", ""),
        status=a.status,
        action_type=a.action_type,
        options=[
            ApprovalOptionDTO(
                key=str(o.get("key", "")),
                label=str(o.get("label", "")),
                detail=str(o.get("detail", "")),
            )
            for o in raw_options
            if isinstance(o, dict) and o.get("key")
        ],
        recommendation=payload.get("recommendation"),
        decision_option=a.decision_option,
        reason=a.reason,
        department_id=str(a.department_id) if a.department_id else None,
        department_name=(resolved.departments.get(a.department_id, "") if a.department_id else ""),
        agent_name=resolved.agents.get(a.agent_id, ""),
        task_id=str(a.task_id) if a.task_id else None,
        task_title=resolved.tasks.get(a.task_id, "") if a.task_id else "",
        created_at=a.created_at.isoformat() if a.created_at else "",
        decided_by_name=resolved.members.get(a.decided_by, "") if a.decided_by else "",
        tool_name=str(tool_name) if isinstance(tool_name, str) else None,
        tool_arguments=raw_arguments if isinstance(raw_arguments, dict) else {},
        title_translations=_i18n_str(payload.get("i18n"), "title"),
        detail_translations=_i18n_str(payload.get("i18n"), "detail"),
        reason_context=a.reason_context,
    )


def clarification_to_dto(row: ClarificationRow) -> ClarificationDTO:
    return ClarificationDTO(
        id=str(row.id),
        run_id=str(row.run_id),
        agent_id=str(row.agent_id),
        agent_name=row.agent_name,
        department_id=str(row.department_id),
        department_name=row.department_name,
        question=row.question,
        status=row.status,
        created_at=row.created_at.isoformat() if row.created_at else "",
    )


def task_row_to_dto(row: TaskRow) -> TaskBoardRowDTO:
    return TaskBoardRowDTO(
        id=str(row.id),
        title=row.title,
        state=row.state,
        column=row.column,
        department_id=str(row.department_id),
        department_name=row.department_name,
        agent_id=str(row.agent_id) if row.agent_id else None,
        agent_name=row.agent_name,
        requested_by_member_id=(
            str(row.requested_by_member_id) if row.requested_by_member_id else None
        ),
        parent_task_id=str(row.parent_task_id) if row.parent_task_id else None,
        delegation_depth=row.delegation_depth,
        created_at=row.created_at.isoformat() if row.created_at else "",
    )


def seat_to_dto(seat: Seat) -> SeatDTO:
    return SeatDTO(
        department_id=str(seat.department_id),
        department_name=seat.department_name,
        seat_role=seat.seat_role,
        agent_manage=seat.agent_manage,
    )


def member_to_dto(row: MemberRow) -> MemberDTO:
    return MemberDTO(
        id=str(row.id),
        subject=row.subject,
        display_name=row.display_name,
        all_departments=row.all_departments,
        first_seen_at=row.first_seen_at.isoformat() if row.first_seen_at else "",
        seats=[seat_to_dto(s) for s in row.seats],
        role_id=None if row.role_id is None else str(row.role_id),
        role_name=row.role_name,
    )


def integration_to_dto(i: m.Integration, slug_to_id: dict[str, str]) -> IntegrationDTO:
    used = [slug_to_id.get(s, s) for s in (i.used_by or [])]
    return IntegrationDTO(
        id=str(i.id),
        name=i.name,
        category=i.category,
        connected=i.connected,
        used_by=used,
        desc=i.description,
        hue=i.hue,
    )


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _dict_or_none(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) and value else None


def model_to_dto(mc: m.ModelConfig, assigned_to: list[str]) -> ModelDTO:
    health: dict[str, Any] = mc.health or {}
    cost: dict[str, Any] = mc.cost_meta or {}
    return ModelDTO(
        id=str(mc.id),
        # Normalize to the registry's canonical provider so the value matches the
        # provider dropdown options (POST already stores canonical; seeded rows may
        # carry a display alias like "Ollama"/"GPT" that must not mis-select the form).
        provider=canonical_provider(mc.provider) or mc.provider,
        name=mc.display_name or mc.model,
        status=health.get("status", "healthy"),
        cost_tier=cost.get("cost_tier", "$"),
        latency=health.get("latency", "—"),
        assigned_to=assigned_to,
        note=cost.get("note", ""),
        model=mc.model,
        locality=mc.locality,
        display_name=mc.display_name,
        context_window=_int_or_none((mc.params or {}).get("context_window")),
        max_tokens=_int_or_none((mc.params or {}).get("max_tokens")),
        supports_vision=bool((mc.params or {}).get("supports_vision", False)),
        effort=_str_or_none((mc.params or {}).get("effort")),
        extra=_dict_or_none((mc.params or {}).get("extra")),
        # `mapped_column(default=False)` applies at INSERT, not at construction; a
        # row built and serialized before its first flush still reads None here.
        used_by_copilot=bool(mc.used_by_copilot),
        credential_id=str(mc.credential_id) if mc.credential_id else None,
        health_error=health.get("error"),
        health_checked_at=health.get("checkedAt"),
    )


def skill_to_dto(s: m.Skill, version: m.SkillVersion | None) -> SkillDTO:
    definition: dict[str, Any] = (version.definition if version else {}) or {}
    pres: dict[str, Any] = definition.get("presentation", {})
    i18n = pres.get("i18n")
    price = pres.get("price")
    return SkillDTO(
        id=str(s.id),
        name=s.name,
        description=s.description,
        category=s.category or "",
        origin=s.origin,
        version=version.semver if version else "",
        author=s.author,
        tools=pres.get("tools", []),
        knowledge=pres.get("knowledge", []),
        guardrails=pres.get("guardrails", []),
        instructions=definition.get("instruction", ""),
        used_by_agents=pres.get("used_by_agents", 0),
        installs=pres.get("installs"),
        price=price,
        updated_at=pres.get("updated_at", ""),
        current_version_id=str(s.current_version_id) if s.current_version_id else None,
        deleted_at=s.deleted_at.isoformat() if s.deleted_at else None,
        name_translations=_i18n_str(i18n, "name"),
        description_translations=_i18n_str(i18n, "description"),
        instructions_translations=_i18n_str(i18n, "instructions"),
        guardrails_translations=_i18n_str_list(i18n, "guardrails"),
    )


def datasource_to_dto(d: m.DataSource) -> DataSourceDTO:
    cfg: dict[str, Any] = d.config or {}
    return DataSourceDTO(
        id=str(d.id),
        kind=d.connector_type,
        name=d.name,
        connected=d.connected,
        last_sync=d.last_sync_at,
        doc_count=d.doc_count or None,
        schedule=cfg.get("schedule"),
        sensitivity=d.classification,
        scope=cfg.get("scope"),
        connector_type=d.connector_type,
        reconcile_state=d.reconcile_state,
        reconcile_note=d.reconcile_note,
        last_sync_status=d.last_sync_status,
        last_sync_error=d.last_sync_error,
        deleted_at=d.deleted_at.isoformat() if d.deleted_at else None,
        config=cfg,
    )


def knowledge_document_to_dto(doc: DocumentSummary) -> KnowledgeDocumentDTO:
    return KnowledgeDocumentDTO(
        source_uri=doc.source_uri,
        data_source_id=str(doc.data_source_id) if doc.data_source_id else None,
        kb_id=str(doc.kb_id),
        chunks=doc.chunks,
        created_at=doc.created_at.isoformat() if doc.created_at else None,
        deleted_at=doc.deleted_at.isoformat() if doc.deleted_at else None,
        deleted_reason=doc.deleted_reason,
        reduced_at=doc.reduced_at.isoformat() if doc.reduced_at else None,
    )


def chunk_to_dto(row: ChunkRow) -> KbChunkDTO:
    return KbChunkDTO(
        id=str(row.id),
        kb_id=str(row.kb_id),
        source_uri=row.source_uri,
        content=row.content,
        classification=row.classification,
        chunk_metadata=row.chunk_metadata,
        created_at=row.created_at.isoformat(),
    )


def similar_chunk_to_dto(row: SimilarChunkRow) -> SimilarChunkDTO:
    return SimilarChunkDTO(
        id=str(row.id),
        kb_id=str(row.kb_id),
        source_uri=row.source_uri,
        content=row.content,
        classification=row.classification,
        chunk_metadata=row.chunk_metadata,
        created_at=row.created_at.isoformat(),
        similarity=row.similarity,
    )


def ingestion_job_to_dto(job: m.IngestionJob) -> IngestionJobDTO:
    return IngestionJobDTO(
        id=str(job.id),
        status=job.status,
        stats=job.stats or {},
    )


def kb_to_dto(
    kb: m.KnowledgeBase,
    linked_departments: list[str],
    linked_agents: list[str],
    linked_source_ids: list[str] | None = None,
) -> KnowledgeBaseDTO:
    """`linked_source_ids` is the caller's own query over live `KbChunk` rows,
    not `kb.source_ids` -- that column is never written anywhere in this
    codebase (create_base doesn't set it, no sync path updates it), so it
    would always serialize as the create-time default. Defaults to `None` ->
    `[]` for call sites that genuinely have nothing to report (a base that
    was just created, before any source has ever synced into it)."""
    fresh: dict[str, Any] = kb.freshness or {}
    i18n = fresh.get("i18n")
    return KnowledgeBaseDTO(
        id=str(kb.id),
        name=kb.name,
        description=kb.description,
        source_ids=linked_source_ids or [],
        docs=fresh.get("docs", 0),
        chunks=fresh.get("chunks", 0),
        embedding_model=kb.embedding_model,
        sensitivity=kb.classification,
        updated=fresh.get("updated", ""),
        status=kb.status,
        linked_departments=linked_departments,
        linked_agents=linked_agents,
        roles=fresh.get("roles", []),
        local_only=kb.local_only,
        deleted_at=kb.deleted_at.isoformat() if kb.deleted_at else None,
        name_translations=_i18n_str(i18n, "name"),
        description_translations=_i18n_str(i18n, "description"),
        index_type=kb.index_type or "internal",
        index_config=dict(kb.index_config or {}),
        credential_id=str(kb.credential_id) if kb.credential_id else None,
    )
