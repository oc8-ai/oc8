"""Everything that touches the dev stack: tenant lookup, throw-away fixtures
(department + agent + MCP connection), run intake through `enqueue_run`, the
wait for a terminal state, and teardown.

Every DB step opens its own `tenant_session`: `enqueue_run` commits inside,
and a commit ends the tenant RLS binding ("tenant GUC dies at commit"), so
nothing here reuses a session across that call.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from oc8 import models as m
from oc8.agent.mcp_env import resolve_mcp_env
from oc8.capas.discovery import resolve_tool_pack_connection
from oc8.db.session import owner_session, tenant_session
from oc8.runtime.intake import enqueue_run
from oc8.runtime.registry import BUILTIN_IN_PROCESS_RUNTIME_REF, BUILTIN_ISOLATED_RUNTIME_REF
from oc8.runtime.states import TERMINAL, RunState
from oc8_evals.mocks._store import ENV_VAR as STATE_FILE_ENV
from oc8_evals.mocks._store import Store
from oc8_evals.odoo import Odoo
from oc8_evals.tasks import Task

#: Where the mocks are importable from inside the containers (compose override).
MOCK_PYTHONPATH = "/app/evals"
MOCK_MODULES = {
    "microsoft365": "oc8_evals.mocks.microsoft365",
    "google_workspace": "oc8_evals.mocks.google_workspace",
    "jira": "oc8_evals.mocks.jira",
}
#: Suite system ids that are not the capa folder name. The connection row
#: still uses the suite id (`name=system`) so tools route as "jira".
_PACK_FOR_SYSTEM = {
    "jira": "jira_mcp",
}
RUNTIME_REFS = {
    "inprocess": BUILTIN_IN_PROCESS_RUNTIME_REF,
    "isolated": BUILTIN_ISOLATED_RUNTIME_REF,
}
#: States that mean "the agent stopped and is waiting for a human" -- terminal
#: for the eval's purposes (a task that expects them scores them as correct).
PARKED = {RunState.WAITING_FOR_INPUT.value, RunState.WAITING_FOR_APPROVAL.value}


@dataclass
class Fixture:
    department_id: uuid.UUID
    agent_id: uuid.UUID
    connection_id: uuid.UUID
    connection_ids: tuple[uuid.UUID, ...] = ()
    task_ids: list[uuid.UUID] = field(default_factory=list)


def should_pin_connection(systems: tuple[str, ...]) -> bool:
    return len(systems) <= 1


def caps_params(task: Task) -> dict:
    params: dict = {}
    if task.code_mode:
        params["code_mode"] = True
    if task.context_window_tokens is not None:
        params["context_window_tokens"] = task.context_window_tokens
    return params


def department_frame_for(task: Task) -> dict[str, Any]:
    """Department frame for one eval attempt.

    code_mode bulk (25 partner writes) cannot fit DEFAULT_RECORDS_PER_RUN=5;
    records_per_run=0 lifts the blast ceiling for that department only.
    """
    frame: dict[str, Any] = {"tools": task.frame_tools}
    if task.code_mode:
        frame["limits"] = {"records_per_run": 0}
    return frame


def agent_definition_for(task: Task) -> dict[str, Any]:
    """Agent definition for one eval attempt.

    Outward grant + clarify flag are fixture choices; B5 thresholds stay as
    in Package 12. Autonomous tasks that must send without parking get an
    explicit b5_grants outward entry. Clarify is off when the task does not
    expect a question (calendar create otherwise invents missing facts).
    """
    definition: dict[str, Any] = {
        "max_steps": task.max_steps,
        "autonomy": task.autonomy,
    }
    if task.autonomy == "autonomous" and not task.expects_approval:
        definition["b5_grants"] = ["outward"]
    if not task.expects_question:
        definition["clarify_before_irreversible"] = False
    return definition


async def assign_skill(db, *, tenant_id, agent_id, definition: dict) -> uuid.UUID:
    import hashlib
    import json

    raw = json.dumps(definition, sort_keys=True).encode()
    skill = m.Skill(
        tenant_id=tenant_id,
        name=definition["slug"],
        description=definition["instruction"],
        author="eval",
        origin="local",
        trust_level="first_party",
    )
    db.add(skill)
    await db.flush()
    version = m.SkillVersion(
        tenant_id=tenant_id,
        skill_id=skill.id,
        semver="1.0.0",
        definition=definition,
        artifact_hash=hashlib.sha256(raw).digest(),
    )
    db.add(version)
    await db.flush()
    skill.current_version_id = version.id
    db.add(
        m.SkillAssignment(
            tenant_id=tenant_id,
            agent_id=agent_id,
            skill_version_id=version.id,
            enabled=True,
            overrides={},
        )
    )
    return version.id


@dataclass
class ScenarioContext:
    tenant_id: uuid.UUID
    run_tag: str
    prefix: str
    odoo: Odoo | None
    mock_state: Store | None
    task: Task
    run_context: dict[str, Any] = field(default_factory=dict)
    final_state: str = ""
    run_id: uuid.UUID | None = None


async def find_tenant(slug: str | None) -> uuid.UUID:
    async with owner_session() as db:
        stmt = select(m.Organization)
        if slug:
            stmt = stmt.where(m.Organization.slug == slug)
        rows = (await db.execute(stmt)).scalars().all()
    if len(rows) != 1:
        names = ", ".join(r.slug for r in rows) or "none"
        raise RuntimeError(f"expected exactly one tenant (got: {names}); pass --tenant <slug>")
    return rows[0].id


async def find_source_connection(tenant_id: uuid.UUID, name: str) -> m.McpConnection:
    async with tenant_session(tenant_id) as db:
        rows = (
            (
                await db.execute(
                    select(m.McpConnection).where(
                        m.McpConnection.tenant_id == tenant_id,
                        m.McpConnection.name == name,
                        m.McpConnection.connected.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
    if not rows:
        raise RuntimeError(f"no connected MCP connection named {name!r} in this tenant")
    return rows[0]


def _mock_connection_config(system: str, state_file: Path) -> tuple[dict[str, Any], Any]:
    pack = _PACK_FOR_SYSTEM.get(system, system)
    manifest = resolve_tool_pack_connection(pack, "primary")
    if manifest is None:
        raise RuntimeError(f"tool pack {system!r} has no 'primary' connection in the manifest")
    if system not in MOCK_MODULES:
        raise RuntimeError(f"no eval mock module registered for system {system!r}")
    config = dict(manifest.config)
    config["_plugin_name"] = pack
    config["_connection_key"] = manifest.key
    config["command"] = "python"
    config["args"] = ["-m", MOCK_MODULES[system]]
    config["env"] = {"PYTHONPATH": MOCK_PYTHONPATH, STATE_FILE_ENV: str(state_file)}
    config["secret_env"] = {}
    return config, manifest.scopes


async def create_fixture(
    tenant_id: uuid.UUID,
    *,
    task: Task,
    runtime: str,
    run_tag: str,
    source_conn: m.McpConnection | None,
    mock_state_file: Path | None,
    model_config_id: uuid.UUID | None,
) -> Fixture:
    async with tenant_session(tenant_id) as db:
        agent_model_config_id = model_config_id
        caps = caps_params(task)
        if caps and model_config_id is not None:
            source = await db.get(m.ModelConfig, model_config_id)
            if source is None:
                raise RuntimeError(f"model config {model_config_id} not found")
            clone = m.ModelConfig(
                tenant_id=tenant_id,
                provider=source.provider,
                model=source.model,
                locality=source.locality,
                credential_id=source.credential_id,
                fallbacks=list(source.fallbacks or []),
                cost_meta=dict(source.cost_meta or {}),
                display_name=f"{source.display_name or source.model} eval",
                health=dict(source.health or {}),
                used_by_copilot=False,
                params={**dict(source.params or {}), **caps},
            )
            db.add(clone)
            await db.flush()
            agent_model_config_id = clone.id

        dept = m.Department(
            tenant_id=tenant_id,
            name=f"EVAL {run_tag} {task.id}",
            frame=department_frame_for(task),
        )
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant_id,
            department_id=dept.id,
            name=f"Eval agent {task.id}",
            role_title="Office agent",
            status="running",
            narrowing={},
            definition=agent_definition_for(task),
            presentation={},
            runtime_ref=RUNTIME_REFS[runtime],
            model_config_id=agent_model_config_id,
        )
        db.add(agent)
        await db.flush()

        connection_ids: list[uuid.UUID] = []
        for system in task.systems:
            if system == "odoo":
                if source_conn is None:
                    raise RuntimeError("odoo task without a source connection")
                config, scopes = dict(source_conn.config or {}), source_conn.scopes
                server_url, transport = source_conn.server_url, source_conn.transport
            else:
                if mock_state_file is None:
                    raise RuntimeError("mock task without a state file")
                config, scopes = _mock_connection_config(system, mock_state_file)
                server_url, transport = "", "stdio"
            conn = m.McpConnection(
                tenant_id=tenant_id,
                department_id=dept.id,
                name=system,
                transport=transport,
                server_url=server_url,
                scopes=scopes,
                config=config,
                connected=True,
                health={},
            )
            db.add(conn)
            await db.flush()
            connection_ids.append(conn.id)

        if task.skill_definition:
            await assign_skill(
                db,
                tenant_id=tenant_id,
                agent_id=agent.id,
                definition=task.skill_definition,
            )

        ids = tuple(connection_ids)
        return Fixture(
            department_id=dept.id,
            agent_id=agent.id,
            connection_id=ids[0],
            connection_ids=ids,
        )


async def odoo_for(tenant_id: uuid.UUID, conn: m.McpConnection) -> Odoo:
    async with tenant_session(tenant_id) as db:
        env = await resolve_mcp_env(
            db, tenant_id=tenant_id, cfg=dict(conn.config or {}), connection_name=conn.name
        )
    return Odoo.from_env(env)


async def start_run(tenant_id: uuid.UUID, fixture: Fixture, task: Task) -> uuid.UUID:
    context: dict[str, Any] = {"task": task.task_text}
    if should_pin_connection(task.systems):
        context["mcp_connection_id"] = str(fixture.connection_id)
    async with tenant_session(tenant_id) as db:
        run, _published = await enqueue_run(
            db,
            tenant_id=tenant_id,
            agent_id=fixture.agent_id,
            context=context,
            # agent_run's ck_agent_run_source CHECK constraint only allows
            # manual/cron/event/webhook/delegation/decision/handoff/chat --
            # "eval" isn't one of them. An eval attempt is, mechanically, a
            # manually-triggered run (invoked from the CLI, not a scheduler
            # or webhook), so "manual" is the correct existing value rather
            # than a new enum member.
            source="manual",
        )
        return run.id


async def wait_for_terminal(
    tenant_id: uuid.UUID, run_id: uuid.UUID, *, timeout_s: int
) -> tuple[str, dict[str, Any]]:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        async with tenant_session(tenant_id) as db:
            run = await db.get(m.AgentRun, run_id)
            if run is None:
                raise RuntimeError(f"run {run_id} vanished")
            state, ctx = str(run.state), dict(run.context or {})
        if state in {s.value for s in TERMINAL} or state in PARKED:
            return state, ctx
        if asyncio.get_event_loop().time() > deadline:
            return "timeout", ctx
        await asyncio.sleep(2)


async def usage_for(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> tuple[int, int, int]:
    async with tenant_session(tenant_id) as db:
        row = (
            await db.execute(
                select(
                    func.coalesce(func.sum(m.TokenUsageRecord.tokens_in), 0),
                    func.coalesce(func.sum(m.TokenUsageRecord.tokens_out), 0),
                    func.coalesce(func.sum(m.TokenUsageRecord.platform_units), 0),
                ).where(m.TokenUsageRecord.agent_id == agent_id)
            )
        ).one()
    return int(row[0]), int(row[1]), int(row[2])


async def teardown_fixture(tenant_id: uuid.UUID, fixture: Fixture) -> None:
    """Best-effort: the department cascade is not relied on. Rows that other
    tables reference (runs, usage, audit) are left in place -- they carry the
    evidence of the run and are tenant-local to the dev stack."""
    async with tenant_session(tenant_id) as db:
        for cid in fixture.connection_ids or (fixture.connection_id,):
            conn = await db.get(m.McpConnection, cid)
            if conn is not None:
                conn.connected = False
        agent = await db.get(m.Agent, fixture.agent_id)
        if agent is not None:
            agent.status = "stopped"
