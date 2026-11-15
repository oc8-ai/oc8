"""Demo showcase extras: costs, statistics, installed capas, flows, handoffs.

Called from `_seed_acme` so a hosted OC8_DEMO walkthrough is not empty on
/costs, /statistics, /capas (Installed), /flows and /handoffs. Numbers and
rows are fiction; they reuse the same deterministic `_det(...)` ids as the
rest of the ACME seed so re-seed stays idempotent where the schema allows.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.capas.discovery import find_plugin
from oc8.capas.lifecycle import consent_required, enable_plugin
from oc8.capas.service import install_plugin
from oc8.collab.flow_engine import start_flow_run
from oc8.collab.flow_spec import parse_flow_spec
from oc8.collab.handoff import (
    create_handoff,
    create_handoff_type,
)
from oc8.seed.model_providers import seed_builtin_model_providers
from oc8.seed.runtime_adapters import seed_runtime_adapters


def _det(tenant_id: uuid.UUID, *parts: Any) -> uuid.UUID:
    # Late import: oc8.seed.__init__ calls into this module.
    from oc8.seed import det

    return det(tenant_id, *parts)


def _agents() -> list[tuple[Any, ...]]:
    from oc8.seed import AGENTS

    return AGENTS

# Capas that make the Installed tab look like a real ACME stack. Skipped when
# the folder is missing on disk (local pytest without the capas mount).
SHOWCASE_CAPAS: tuple[str, ...] = (
    "hubspot_mcp",
    "odoo_mcp",
    "microsoft365",
    "github_mcp",
    "jira_mcp",
    "telegram_approvals",
)

# Agent slug -> (provider, model) matching model_price patterns in migrations.
_AGENT_MODEL: dict[str, tuple[str, str]] = {
    "vera": ("anthropic", "claude-3-5-sonnet"),
    "fin": ("anthropic", "claude-3-5-sonnet"),
    "sam": ("anthropic", "claude-3-5-sonnet"),
    "dex": ("anthropic", "claude-3-5-sonnet"),
    "mara": ("anthropic", "claude-3-5-sonnet"),
    "data": ("openai", "gpt-4o"),
    "ada": ("openai", "gpt-4o"),
    "tim": ("openai", "gpt-4o"),
    "hera": ("openai_compatible", "mistral-large"),  # local llama has no price row
    "doku": ("openai_compatible", "mistral-large"),
    "cent": ("openai_compatible", "mistral-large"),
    "kern": ("openai_compatible", "mistral-large"),
    "leo": ("openai", "gpt-4o-mini"),
    "nina": ("anthropic", "claude-3-5-haiku"),
    "ops": ("openai", "gpt-4o-mini"),
    "echo": ("openai", "gpt-4o-mini"),
}


def _hash_spec(spec: dict[str, Any]) -> bytes:
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).digest()


async def seed_showcase_capas(session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
    """Install + enable first-party capas so Capas → Installed is populated."""
    await seed_builtin_model_providers(session, tenant_id=tenant_id)
    await seed_runtime_adapters(session, tenant_id=tenant_id)

    for name in SHOWCASE_CAPAS:
        existing = (
            await session.execute(
                select(m.Capa).where(m.Capa.name == name, m.Capa.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            # Still ensure enabled (sales-department style installs skip enable).
            inst = (
                await session.execute(
                    select(m.CapaInstallation).where(
                        m.CapaInstallation.tenant_id == tenant_id,
                        m.CapaInstallation.capa_id == existing.id,
                    )
                )
            ).scalar_one_or_none()
            if inst is not None and inst.status == "enabled":
                continue
            version = (
                await session.execute(
                    select(m.CapaVersion).where(m.CapaVersion.id == existing.current_version_id)
                )
            ).scalar_one_or_none()
            if version is None:
                continue
            await enable_plugin(
                session,
                tenant_id=tenant_id,
                capa_id=existing.id,
                granted_permissions=consent_required(version),
            )
            continue

        discovered = find_plugin(name)
        if discovered is None or not discovered.valid or discovered.manifest is None:
            continue
        try:
            version = await install_plugin(
                session, tenant_id=tenant_id, manifest_data=discovered.manifest
            )
            await enable_plugin(
                session,
                tenant_id=tenant_id,
                capa_id=version.capa_id,
                granted_permissions=consent_required(version),
            )
        except Exception as err:  # noqa: BLE001 — showcase seed must not abort ACME
            print(f"demo seed: skipped capa {name}: {err}")
            continue

    # Enable the department template already installed by seed_department_templates.
    sales = (
        await session.execute(
            select(m.Capa).where(
                m.Capa.name == "sales-department", m.Capa.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if sales is not None and sales.current_version_id is not None:
        version = await session.get(m.CapaVersion, sales.current_version_id)
        if version is not None:
            await enable_plugin(
                session,
                tenant_id=tenant_id,
                capa_id=sales.id,
                granted_permissions=consent_required(version),
            )

    # Enable materialises tool_pack McpConnection rows (connected=False,
    # health={}) -- these used to be deleted here because they rendered as
    # stray "Untested" cards under Capas. capas.tsx now folds a connection
    # with a `pluginName` stamp into its own Capa's card (ConnectionDot)
    # instead of listing it separately, so that's no longer a concern. Kept
    # rows matter beyond that page too: without them `GET /mcp/connections`
    # is empty in the hosted demo, which silently blanks the agent-level
    # guardrail editor (it only renders once a real `connection` object
    # exists for a tool key) and disables "+ Add tool" entirely.
    await session.flush()


async def seed_demo_usage(session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
    """Token usage + budgets for the Costs page."""
    existing = (
        await session.execute(
            select(m.TokenUsageRecord.id)
            .where(m.TokenUsageRecord.tenant_id == tenant_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return

    now = dt.datetime.now(tz=dt.UTC)
    # Spread ~14 days of traffic; scale loosely with seeded tasks_today.
    for slug, _name, _role, _llm, _prov, _status, _tools, _la, _lr, tasks_today, *_ in _agents():
        provider, model = _AGENT_MODEL.get(slug, ("openai", "gpt-4o-mini"))
        dept_slug = next(row[13] for row in _agents() if row[0] == slug)
        agent_id = _det(tenant_id, "agent", slug)
        dept_id = _det(tenant_id, "dept", dept_slug)
        days = max(3, min(10, tasks_today // 3 or 3))
        for day in range(days):
            tin = 8_000 + tasks_today * 400 + day * 900
            tout = 2_000 + tasks_today * 80 + day * 200
            cache = day == 0 and slug == "vera"
            session.add(
                m.TokenUsageRecord(
                    id=_det(tenant_id, "usage", slug, day),
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    department_id=dept_id,
                    provider=provider,
                    model=model,
                    tokens_in=tin,
                    tokens_out=tout,
                    cache_hit=cache,
                    saved_tokens_in=12_000 if cache else 0,
                    saved_tokens_out=3_000 if cache else 0,
                    platform_units=0,
                    request_id=_det(tenant_id, "usage-req", slug, day),
                    ts=now - dt.timedelta(days=day, hours=2 + (day % 5)),
                )
            )

    session.add(
        m.Budget(
            id=_det(tenant_id, "budget", "tenant"),
            tenant_id=tenant_id,
            department_id=None,
            soft_limit_tokens=5_000_000,
            hard_limit_tokens=8_000_000,
            dollar_budget_usd=500.0,
            dollar_reference_provider="anthropic",
            dollar_reference_model="claude-3-5-sonnet",
        )
    )
    session.add(
        m.Budget(
            id=_det(tenant_id, "budget", "vertrieb"),
            tenant_id=tenant_id,
            department_id=_det(tenant_id, "dept", "vertrieb"),
            soft_limit_tokens=800_000,
            hard_limit_tokens=1_200_000,
        )
    )
    await session.flush()


async def seed_demo_runs(session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
    """Agent runs + state transitions for the Statistics charts."""
    existing = (
        await session.execute(
            select(m.AgentRun.id).where(m.AgentRun.tenant_id == tenant_id).limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return

    now = dt.datetime.now(tz=dt.UTC)
    # A few representative agents keep the default groupBy=agent chart readable.
    showcase = (
        ("vera", 8, "done"),
        ("vera", 1, "waiting_for_approval"),
        ("fin", 6, "done"),
        ("fin", 1, "failed"),
        ("dex", 7, "done"),
        ("nina", 5, "done"),
        ("echo", 9, "done"),
        ("ops", 2, "failed"),
        ("mara", 4, "done"),
        ("ada", 5, "done"),
    )
    for slug, count, terminal in showcase:
        agent_id = _det(tenant_id, "agent", slug)
        for i in range(count):
            run_id = _det(tenant_id, "run", slug, terminal, i)
            t0 = now - dt.timedelta(days=(i % 12), hours=1 + i)
            session.add(
                m.AgentRun(
                    id=run_id,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    state=terminal,
                    source="manual",
                    cursor={},
                    messages=[],
                    context={"seed": True},
                    evidence_state="none",
                    created_at=t0,
                    updated_at=t0 + dt.timedelta(minutes=18),
                )
            )
            # Timeline so duration KPIs are non-null.
            steps: list[tuple[str | None, str, int]] = [
                (None, "queued", 0),
                ("queued", "running", 2),
            ]
            if terminal == "waiting_for_approval":
                steps += [
                    ("running", "waiting_for_approval", 45),
                ]
            elif terminal == "failed":
                steps += [("running", "failed", 90)]
            else:
                steps += [
                    ("running", "waiting_for_approval", 40),
                    ("waiting_for_approval", "running", 40 + 12 * 60),
                    ("running", "done", 40 + 12 * 60 + 90),
                ]
            for from_s, to_s, offset_s in steps:
                session.add(
                    m.RunStateTransition(
                        id=_det(tenant_id, "rst", slug, terminal, i, to_s, offset_s),
                        tenant_id=tenant_id,
                        run_id=run_id,
                        from_state=from_s,
                        to_state=to_s,
                        at=t0 + dt.timedelta(seconds=offset_s),
                    )
                )
    await session.flush()


async def seed_demo_collab(session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
    """Handoff types, Quote-to-Cash flow, runs, and showcase handoffs."""
    existing = (
        await session.execute(select(m.Flow.id).where(m.Flow.tenant_id == tenant_id).limit(1))
    ).scalar_one_or_none()
    if existing is not None:
        return

    vertrieb = _det(tenant_id, "dept", "vertrieb")
    entwicklung = _det(tenant_id, "dept", "entwicklung")
    buchhaltung = _det(tenant_id, "dept", "buchhaltung")
    actor = _det(tenant_id, "actor", "collab-seed")

    schemas: dict[str, dict[str, Any]] = {
        "project.kickoff": {
            "type": "object",
            "properties": {
                "customer": {"type": "string"},
                "scope": {"type": "array"},
                "budget_eur": {"type": "number"},
                "deadline": {"type": "string"},
            },
            "required": ["customer", "scope", "budget_eur"],
        },
        "dev.project.start": {
            "type": "object",
            "properties": {
                "customer": {"type": "string"},
                "milestone": {"type": "string"},
                "budget_eur": {"type": "number"},
            },
            "required": ["customer"],
        },
        "finance.invoice.create": {
            "type": "object",
            "properties": {
                "customer": {"type": "string"},
                "amount_eur": {"type": "number"},
                "project_ref": {"type": "string"},
            },
            "required": ["customer", "amount_eur", "project_ref"],
        },
        "customer.welcome": {
            "type": "object",
            "properties": {"customer": {"type": "string"}, "plan": {"type": "string"}},
            "required": ["customer"],
        },
    }
    types: dict[str, m.HandoffType] = {}
    for name, schema in schemas.items():
        types[name] = await create_handoff_type(
            session, tenant_id=tenant_id, name=name, payload_schema=schema
        )

    q2c_spec = {
        "id": "quote-to-cash",
        "version": "1.0.0",
        "trigger": {"event": "sales.deal.won", "from_department_id": str(vertrieb)},
        "stages": [
            {
                "id": "kickoff",
                "handoff": {
                    "type": "project.kickoff",
                    "to_department_id": str(entwicklung),
                    "gate": "approval",
                    "payload_map": {
                        "customer": "$.customer",
                        "scope": "$.scope",
                        "budget_eur": "$.budget_eur",
                        "deadline": "$.deadline",
                    },
                },
            },
            {
                "id": "build",
                "after": "kickoff.completed",
                "handoff": {
                    "type": "dev.project.start",
                    "to_department_id": str(entwicklung),
                    "payload_map": {
                        "customer": "$.customer",
                        "milestone": "$.milestone",
                        "budget_eur": "$.budget_eur",
                    },
                },
            },
            {
                "id": "invoice",
                "after": "build.completed",
                "handoff": {
                    "type": "finance.invoice.create",
                    "to_department_id": str(buchhaltung),
                    "payload_map": {
                        "customer": "$.customer",
                        "amount_eur": "$.budget_eur",
                        "project_ref": "$.project_ref",
                    },
                },
            },
        ],
    }
    onboard_spec = {
        "id": "customer-onboarding",
        "version": "1.0.0",
        "trigger": {"event": "sales.deal.won", "from_department_id": str(vertrieb)},
        "stages": [
            {
                "id": "welcome",
                "handoff": {
                    "type": "customer.welcome",
                    "to_department_id": str(vertrieb),
                    "payload_map": {"customer": "$.customer", "plan": "$.plan"},
                },
            }
        ],
    }

    for name, spec in (
        ("Quote-to-Cash", q2c_spec),
        ("Customer Onboarding", onboard_spec),
    ):
        flow = m.Flow(id=_det(tenant_id, "flow", spec["id"]), tenant_id=tenant_id, name=name)
        session.add(flow)
        await session.flush()
        ver = m.FlowVersion(
            id=_det(tenant_id, "flowver", spec["id"], spec["version"]),
            tenant_id=tenant_id,
            flow_id=flow.id,
            semver=spec["version"],
            spec=spec,
            artifact_hash=_hash_spec(spec),
        )
        session.add(ver)
        await session.flush()
        flow.current_version_id = ver.id

    q2c_ver = await session.get(m.FlowVersion, _det(tenant_id, "flowver", "quote-to-cash", "1.0.0"))
    assert q2c_ver is not None
    parsed = parse_flow_spec(q2c_ver.spec)
    # Live run → pending kickoff for Bauer GmbH (mirrors collaboration.ts ho-1).
    await start_flow_run(
        session,
        tenant_id=tenant_id,
        flow_version=q2c_ver,
        spec=parsed,
        context={
            "customer": "Bauer GmbH",
            "scope": ["Onboarding portal", "SSO", "Reporting v1"],
            "budget_eur": 74000,
            "deadline": "2026-09-30",
            "milestone": "M1 — Setup",
            "project_ref": "P-2026-0714",
        },
    )

    # Extra handoffs so the Handoffs page has in_progress / completed rows
    # beyond what the live flow run just created. Status is set directly (no
    # accept/complete helpers) so seed does not publish on the realtime bus.
    ho_build = await create_handoff(
        session,
        tenant_id=tenant_id,
        handoff_type=types["dev.project.start"],
        source_department_id=entwicklung,
        target_department_id=entwicklung,
        payload={
            "customer": "Nordheim AG",
            "milestone": "M1 — Setup",
            "budget_eur": 42000,
        },
        created_by=actor,
        gate="auto",
    )
    ho_build.status = "in_progress"

    ho_inv = await create_handoff(
        session,
        tenant_id=tenant_id,
        handoff_type=types["finance.invoice.create"],
        source_department_id=entwicklung,
        target_department_id=buchhaltung,
        payload={
            "customer": "Meier AG",
            "amount_eur": 28900,
            "project_ref": "P-2026-004",
        },
        created_by=actor,
        gate="auto",
    )
    ho_inv.status = "completed"

    await session.flush()


async def seed_demo_showcase(session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
    """All demo extras for costs / statistics / capas / flows / handoffs."""
    await seed_showcase_capas(session, tenant_id=tenant_id)
    await seed_demo_usage(session, tenant_id=tenant_id)
    await seed_demo_runs(session, tenant_id=tenant_id)
    await seed_demo_collab(session, tenant_id=tenant_id)
