# backend/src/oc8/agent/assistant.py
"""The one, tenant-wide 'oc8 Assistant' agent (design doc
2026-08-31-unified-assistant-telegram-chat-design.md).

Lazily provisioned: the first caller (a web Chat request, a Telegram
free-text message) that needs it creates it. No migration data-fix needed for
existing tenants.

Read-only + delegate_task + propose_change + decide_approval. A write-capable
tool is never granted to this agent unless it resolves the acting human's own
`DepartmentScope` and calls the exact same service-layer function a
human-facing route would call for that action -- "same actor, same function"
(see docs/superpowers/specs/2026-09-05-copilot-access-and-hard-permissions-design.md).
`propose_change` never applies anything itself; `decide_approval` calls
`oc8.approvals.service.decide_approval` with a resolved `AgentActor`, so RBAC
is inherited from that funnel, not reimplemented here.
"""

from __future__ import annotations

import contextlib
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.versioning import NoChangesToPublish, publish_version

ASSISTANT_NAME = "oc8 Assistant"
ASSISTANT_DEPARTMENT_NAME = "oc8 Assistant"

_MISSION = """Du bist der oc8 Assistant -- der zentrale Ansprechpartner für \
diesen Tenant, per Web-Chat und per Telegram erreichbar.

Beantworte Fragen direkt, wenn du sie mit deinen eigenen Werkzeugen \
beantworten kannst. Für Arbeit, die zu einem bestimmten Fachbereich gehört \
(z. B. Tickets bearbeiten), delegiere sie per delegate_task an den \
zuständigen Agent aus der Liste, die du mit jeder Anfrage bekommst -- du \
erledigst sie nicht selbst. Du bekommst dabei ALLE Agents im Tenant \
genannt, nicht nur die aus einer eigenen Abteilung (du hast keine \
Kollegen) -- wähle anhand von Name und Rolle den passenden aus und \
delegiere direkt, ohne vorher nachzufragen, ob es dafür jemanden gibt. \
Findet sich niemand Passendes, sag das offen, statt den Menschen nach dem \
Aufbau seines eigenen Systems zu fragen -- die Liste, die du bekommst, IST \
der aktuelle Aufbau.

Du kannst keine Rückfrage stellen und auf eine Antwort warten -- manche \
deiner Gesprächspartner (z. B. über Telegram) haben keine Möglichkeit, dir \
zu antworten, während du wartest. Triff die beste Entscheidung mit dem, \
was du hast, statt zu fragen.

Umfasst eine Aufgabe eine Liste einzelner Punkte (z. B. mehrere Tickets, \
mehrere Datensätze), teile sie selbst in mehrere kleinere delegate_task-\
Aufrufe auf, statt alles in einem einzigen zu bündeln -- z. B. einen Aufruf \
pro Punkt oder in kleinen Gruppen. Ein einzelner Lauf hat ein begrenztes \
Schritte-Budget; zu viele Punkte in einer Aufgabe lassen den ausführenden \
Agent dieses Budget aufbrauchen, bevor alles erledigt ist.

Für strukturelle Änderungen am System (neues Department, neuer Agent, \
Mission ändern, Plugin aktivieren, Integration vorbereiten, eine Guardrail \
für ein Tool eines Agenten setzen -- z. B. "Sina darf externe Nachrichten \
nur mit Freigabe senden") rufst du propose_change auf. Das legt nur einen \
Vorschlag an, den ein Mensch in oc8 noch bestätigen muss -- du führst \
solche Änderungen NIE selbst aus.

Wenn ein Mensch dich bittet, eine offene Freigabe zu entscheiden (z. B. \
"genehmige das" oder "lehne das ab"), rufst du decide_approval auf. Das \
funktioniert nur für Freigaben, die dieser Mensch auch selbst entscheiden \
dürfte -- wird dir das verweigert, sag das offen, statt es erneut zu \
versuchen."""


async def _select_model_config(db: AsyncSession, *, tenant_id: uuid.UUID) -> uuid.UUID | None:
    """Same selection rule as the retired copilot/chat.py:_select_copilot_model
    -- the used_by_copilot-flagged config wins, else the oldest configured one."""
    flagged = await db.scalar(
        select(m.ModelConfig)
        .where(
            m.ModelConfig.tenant_id == tenant_id,
            m.ModelConfig.provider != "",
            m.ModelConfig.model != "",
            m.ModelConfig.used_by_copilot.is_(True),
        )
        .order_by(m.ModelConfig.created_at)
        .limit(1)
    )
    if flagged is not None:
        return flagged.id
    fallback = await db.scalar(
        select(m.ModelConfig)
        .where(
            m.ModelConfig.tenant_id == tenant_id,
            m.ModelConfig.provider != "",
            m.ModelConfig.model != "",
        )
        .order_by(m.ModelConfig.created_at)
        .limit(1)
    )
    return fallback.id if fallback is not None else None


async def _load_assistant(db: AsyncSession, *, tenant_id: uuid.UUID) -> m.Agent | None:
    """The tenant's Assistant row, oldest first.

    `.order_by(created_at).limit(1)` is belt and braces for a database that
    already holds duplicates from before migration 0082's partial unique index
    existed: without an ordering, `db.scalar(select(...))` returns whichever row
    the plan happens to yield, so two consecutive calls in one dev tenant could
    answer with two different agents. The index stops NEW duplicates; this makes
    the answer deterministic for any that are already there.
    """
    row: m.Agent | None = await db.scalar(
        select(m.Agent)
        .where(
            m.Agent.tenant_id == tenant_id,
            m.Agent.is_tenant_assistant.is_(True),
            m.Agent.deleted_at.is_(None),
        )
        .order_by(m.Agent.created_at)
        .limit(1)
    )
    return row


async def _sync_model_config(db: AsyncSession, agent: m.Agent, *, tenant_id: uuid.UUID) -> None:
    """Keep an existing Assistant pointed at whatever ModelConfig currently
    carries `used_by_copilot`.

    Without this the Assistant is frozen on whichever config was flagged at the
    moment it was first lazily created: changing the flag in Settings -> Models
    would change every other copilot surface and silently not this one, for ever.
    Cheap enough to run on the hot path (`get_or_create_assistant` is called on
    every web AND Telegram interaction) -- one indexed lookup, and an assignment
    only when the answer actually moved.
    """
    wanted = await _select_model_config(db, tenant_id=tenant_id)
    # `wanted is None` means the tenant has no usable ModelConfig at all --
    # never clear a working pin because of a transiently empty answer.
    if wanted is None or wanted == agent.model_config_id:
        return
    agent.model_config_id = wanted
    await db.flush()
    # Runs execute the agent's published version, not its row -- so the switch
    # only reaches the Assistant's next run once it is published. The Assistant
    # has no operator-edited draft to protect, so it publishes straight away.
    # NoChangesToPublish only if the row had drifted from its version and this
    # switch brought it back -- then the version already says what it should.
    with contextlib.suppress(NoChangesToPublish):
        await publish_version(db, agent, note="model follows the copilot model setting")


async def get_or_create_assistant(db: AsyncSession, *, tenant_id: uuid.UUID) -> m.Agent:
    existing = await _load_assistant(db, tenant_id=tenant_id)
    if existing is not None:
        await _sync_model_config(db, existing, tenant_id=tenant_id)
        return existing

    # Create inside a SAVEPOINT, exactly as `runtime/intake.enqueue_run` does
    # for `idempotency_key`. There are five concurrent first-call entrypoints
    # now (GET /assistant, the three /chat/sessions... routes via
    # `_assistant_visible`, and `bind_from_free_text`), so two tabs on a fresh
    # tenant genuinely race here. The partial unique index from migration 0082
    # turns the loser into an IntegrityError; only the savepoint unwinds, which
    # leaves the outer transaction -- and its transaction-local RLS tenant
    # binding -- alive, so the follow-up SELECT can still see the winner.
    try:
        async with db.begin_nested():
            dept = m.Department(
                tenant_id=tenant_id,
                name=ASSISTANT_DEPARTMENT_NAME,
                goal="",
                is_assistant_department=True,
            )
            db.add(dept)
            await db.flush()

            agent = m.Agent(
                tenant_id=tenant_id,
                department_id=dept.id,
                name=ASSISTANT_NAME,
                mission=_MISSION,
                is_team_lead=True,
                is_tenant_assistant=True,
                status="running",
                model_config_id=await _select_model_config(db, tenant_id=tenant_id),
            )
            db.add(agent)
            await db.flush()
            # v1 inside the same savepoint, so a lost race (or a failed
            # publish) unwinds the version together with the agent row.
            await publish_version(db, agent)

            # Same convention as capas/service.py's department-with-a-lead
            # creation: collab/intake.py's route_to_team_lead reads
            # dept.team_lead_agent_id and treats None as "no lead -- drop the
            # work silently," so the department this Assistant just got created
            # in must point back at it.
            dept.team_lead_agent_id = agent.id
            await db.flush()
    except IntegrityError:
        # The savepoint took the half-built Department down with the Agent, so
        # the loser leaves nothing behind -- it just adopts the winner's row.
        winner = await _load_assistant(db, tenant_id=tenant_id)
        if winner is None:  # pragma: no cover - not the index we raced on
            raise
        await _sync_model_config(db, winner, tenant_id=tenant_id)
        return winner
    return agent
