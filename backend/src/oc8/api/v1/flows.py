"""Flow publish + run endpoints (§14a.5). Declarative orchestration; the engine
creates handoffs and tracks state but executes no work itself."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.authz.permissions import FLOW, MANAGE, RUN_CONTROL, RUN_START, VIEW, perm
from oc8.collab.flow_engine import FlowError, cancel_flow_run, start_flow_run
from oc8.collab.flow_spec import FlowSpecError, parse_flow_spec
from oc8.schemas.base import CamelModel

router = APIRouter()


class PublishFlowRequest(CamelModel):
    spec: dict[str, Any]


class StartFlowRequest(CamelModel):
    context: dict[str, Any] = {}


class FlowVersionDTO(CamelModel):
    id: str
    flow_id: str
    name: str
    semver: str


class FlowRunDTO(CamelModel):
    id: str
    flow_version_id: str
    flow_id: str = ""
    status: str
    current_stages: list[Any]
    context: dict[str, Any] = {}
    trigger_event: str | None = None


class FlowListDTO(CamelModel):
    id: str
    name: str
    semver: str
    spec: dict[str, Any]
    run_count: int


class FlowDTO(CamelModel):
    id: str
    name: str
    archived: bool


def _hash(spec: dict[str, Any]) -> bytes:
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).digest()


@router.post(
    "/flows",
    response_model=FlowVersionDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(FLOW, MANAGE)))],
)
async def publish(
    body: PublishFlowRequest, db: DbSession, principal: CurrentPrincipal
) -> FlowVersionDTO:
    try:
        spec = parse_flow_spec(body.spec)
    except FlowSpecError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    flow = (
        await db.execute(
            select(m.Flow).where(m.Flow.tenant_id == principal.tenant_id, m.Flow.name == spec.id)
        )
    ).scalar_one_or_none()
    if flow is None:
        flow = m.Flow(tenant_id=principal.tenant_id, name=spec.id)
        db.add(flow)
        await db.flush()

    existing = (
        await db.execute(
            select(m.FlowVersion).where(
                m.FlowVersion.flow_id == flow.id, m.FlowVersion.semver == spec.version
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"flow version {spec.version} already exists"
        )

    version = m.FlowVersion(
        tenant_id=principal.tenant_id,
        flow_id=flow.id,
        semver=spec.version,
        spec=body.spec,
        artifact_hash=_hash(body.spec),
    )
    db.add(version)
    await db.flush()
    flow.current_version_id = version.id
    # Publishing a new version of an archived flow brings it back: the name is
    # unique per tenant, so this is the only way to reuse it.
    flow.archived_at = None
    await db.flush()
    return FlowVersionDTO(
        id=str(version.id), flow_id=str(flow.id), name=flow.name, semver=version.semver
    )


@router.post(
    "/flow-versions/{version_id}/start",
    response_model=FlowRunDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(RUN_START))],
)
async def start(
    version_id: uuid.UUID, body: StartFlowRequest, db: DbSession, principal: CurrentPrincipal
) -> FlowRunDTO:
    version = await db.get(m.FlowVersion, version_id)
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "flow version not found")
    flow = await db.get(m.Flow, version.flow_id)
    if flow is not None and flow.archived_at is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "this flow is archived; publish it again to start it"
        )
    spec = parse_flow_spec(version.spec)
    try:
        run = await start_flow_run(
            db, tenant_id=principal.tenant_id, flow_version=version, spec=spec, context=body.context
        )
    except FlowError as exc:
        # A stage naming a handoff type that no longer exists, or a condition
        # the engine cannot read: the caller's flow, not a server fault.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return await _run_dto(db, run)


@router.get(
    "/flow-runs/{run_id}",
    response_model=FlowRunDTO,
    dependencies=[Depends(require_permission(perm(FLOW, VIEW)))],
)
async def get_run(run_id: uuid.UUID, db: DbSession, principal: CurrentPrincipal) -> FlowRunDTO:
    run = await db.get(m.FlowRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "flow run not found")
    return await _run_dto(db, run)


@router.post(
    "/flow-runs/{run_id}/cancel",
    response_model=FlowRunDTO,
    dependencies=[Depends(require_permission(RUN_CONTROL))],
)
async def cancel_run(run_id: uuid.UUID, db: DbSession, principal: CurrentPrincipal) -> FlowRunDTO:
    """Stop a running flow: its open handoffs are rejected and the agent runs
    working them cancelled. 409 once the run has ended."""
    run = await db.get(m.FlowRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "flow run not found")
    try:
        await cancel_flow_run(db, run, principal=principal)
    except FlowError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return await _run_dto(db, run)


@router.get(
    "/flow-runs",
    response_model=list[FlowRunDTO],
    dependencies=[Depends(require_permission(perm(FLOW, VIEW)))],
)
async def list_runs(db: DbSession, principal: CurrentPrincipal) -> list[FlowRunDTO]:
    rows = (await db.execute(select(m.FlowRun))).scalars().all()
    return [await _run_dto(db, r) for r in rows]


@router.get(
    "/flows",
    response_model=list[FlowListDTO],
    dependencies=[Depends(require_permission(perm(FLOW, VIEW)))],
)
async def list_flows(db: DbSession, principal: CurrentPrincipal) -> list[FlowListDTO]:
    flows = (await db.execute(select(m.Flow).where(m.Flow.archived_at.is_(None)))).scalars().all()
    out: list[FlowListDTO] = []
    for flow in flows:
        version = (
            await db.get(m.FlowVersion, flow.current_version_id)
            if flow.current_version_id
            else None
        )
        if version is None:
            continue
        run_count = (
            await db.execute(
                select(func.count())
                .select_from(m.FlowRun)
                .where(m.FlowRun.flow_version_id == version.id)
            )
        ).scalar_one()
        out.append(
            FlowListDTO(
                id=str(flow.id),
                name=flow.name,
                semver=version.semver,
                spec=version.spec,
                run_count=int(run_count),
            )
        )
    return out


@router.post(
    "/flows/{flow_id}/archive",
    response_model=FlowDTO,
    dependencies=[Depends(require_permission(perm(FLOW, MANAGE)))],
)
async def archive_flow(flow_id: uuid.UUID, db: DbSession, principal: CurrentPrincipal) -> FlowDTO:
    """Hide a flow and stop new runs of it. Runs already going are left alone
    (cancel them one by one), and their history stays readable. Idempotent."""
    flow = await db.get(m.Flow, flow_id)
    if flow is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "flow not found")
    if flow.archived_at is None:
        flow.archived_at = dt.datetime.now(tz=dt.UTC)
        await db.flush()
    return FlowDTO(id=str(flow.id), name=flow.name, archived=True)


async def _run_dto(db: DbSession, run: m.FlowRun) -> FlowRunDTO:
    version = await db.get(m.FlowVersion, run.flow_version_id)
    return FlowRunDTO(
        id=str(run.id),
        flow_version_id=str(run.flow_version_id),
        flow_id=str(version.flow_id) if version is not None else "",
        status=run.status,
        current_stages=list(run.current_stages),
        context=dict(run.context),
        trigger_event=run.trigger_event,
    )
