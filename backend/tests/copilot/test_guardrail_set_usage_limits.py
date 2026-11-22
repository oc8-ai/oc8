"""The Copilot's `agent.guardrail.set` and usage limits.

It must keep a limit an admin already set when it edits another guardrail on
the same connection -- the operation rebuilds the agent's policy, and a
field-by-field rebuild silently drops whatever it does not name -- and it may
propose a limit itself, through the same operation.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from oc8 import models as m
from oc8.auth import Principal
from oc8.authz import pdp
from oc8.config import get_settings
from oc8.copilot.proposals import apply_proposal, create_proposal

_CAPAS_DIR = Path(__file__).resolve().parents[3] / "capas"
SENDING = {"tools": ["mail_send", "mail_reply", "mail_forward"], "max": 50, "per": "day"}


@pytest.fixture(autouse=True)
def _capas_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("OC8_CAPAS_PATH", str(_CAPAS_DIR))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _actor(tenant: uuid.UUID) -> Principal:
    return Principal(subject="operator-1", tenant_id=tenant, role="org_admin")


async def _mail_agent(db: Any, tenant: uuid.UUID, narrowing: dict[str, Any]) -> m.Agent:
    department = m.Department(
        tenant_id=tenant,
        name=f"D-{uuid.uuid4().hex}",
        frame={"tools": {"email": {"enabled": True, "read": True, "modify": True}}},
    )
    db.add(department)
    await db.flush()
    agent = m.Agent(tenant_id=tenant, department_id=department.id, name="Mila", narrowing=narrowing)
    conn = m.McpConnection(
        tenant_id=tenant,
        name="email",
        server_url="",
        transport="stdio",
        config={"_plugin_name": "email", "_connection_key": "primary"},
    )
    db.add_all([agent, conn])
    await db.flush()
    return agent


async def _apply(db: Any, tenant: uuid.UUID, operation: dict[str, Any]) -> None:
    actor = _actor(tenant)
    proposal = await create_proposal(db, actor, [operation])
    assert (await apply_proposal(db, proposal.id, actor)).status == "applied"


async def test_a_guardrail_edit_keeps_an_existing_limit(app_session, acme_tenant) -> None:  # type: ignore[no-untyped-def]
    async with app_session(acme_tenant) as db:
        agent = await _mail_agent(
            db,
            acme_tenant,
            {
                "tools": {
                    "email": {
                        "enabled": True,
                        "read": True,
                        "modify": True,
                        "limits": [SENDING],
                    }
                }
            },
        )
        await _apply(
            db,
            acme_tenant,
            {
                "type": "agent.guardrail.set",
                "agentId": str(agent.id),
                "connectionName": "email",
                "function": "mail_delete",
                "decision": "not_allowed",
            },
        )
        policy = pdp.ToolPolicy.from_json(agent.narrowing["tools"]["email"])
    assert [lim.to_json() for lim in policy.limits] == [SENDING]
    assert policy.only is not None and "mail_delete" not in policy.only


async def test_the_copilot_can_propose_a_limit(app_session, acme_tenant) -> None:  # type: ignore[no-untyped-def]
    async with app_session(acme_tenant) as db:
        agent = await _mail_agent(db, acme_tenant, {})
        await _apply(
            db,
            acme_tenant,
            {
                "type": "agent.guardrail.set",
                "agentId": str(agent.id),
                "connectionName": "email",
                "function": "mail_send",
                "decision": "self_sufficient",
                "usageLimit": {
                    "tools": ["mail_send", "mail_reply", "mail_forward"],
                    "max": 50,
                    "per": "day",
                },
            },
        )
        # Proposing it again with another max replaces it, never doubles it.
        await _apply(
            db,
            acme_tenant,
            {
                "type": "agent.guardrail.set",
                "agentId": str(agent.id),
                "connectionName": "email",
                "function": "mail_send",
                "decision": "self_sufficient",
                "usageLimit": {"tools": ["mail_reply", "mail_send", "mail_forward"], "max": 20},
            },
        )
        policy = pdp.ToolPolicy.from_json(agent.narrowing["tools"]["email"])
    assert [lim.to_json() for lim in policy.limits] == [
        {"tools": ["mail_reply", "mail_send", "mail_forward"], "max": 20, "per": "day"}
    ]


@pytest.mark.parametrize(
    "usage_limit",
    [
        # Must count the function the operation is about.
        {"tools": ["mail_reply"], "max": 5},
        # Only tools this connection has.
        {"tools": ["mail_send", "rm_rf"], "max": 5},
        {"tools": ["mail_send"], "max": -1},
        {"tools": ["mail_send"], "max": 5, "per": "week"},
    ],
)
async def test_a_bad_limit_is_refused(app_session, acme_tenant, usage_limit) -> None:  # type: ignore[no-untyped-def]
    from oc8.copilot.proposals import ProposalRejected

    async with app_session(acme_tenant) as db:
        agent = await _mail_agent(db, acme_tenant, {})
        actor = _actor(acme_tenant)
        operation = {
            "type": "agent.guardrail.set",
            "agentId": str(agent.id),
            "connectionName": "email",
            "function": "mail_send",
            "decision": "self_sufficient",
            "usageLimit": usage_limit,
        }
        with pytest.raises((ValueError, ProposalRejected)):
            proposal = await create_proposal(db, actor, [operation])
            await apply_proposal(db, proposal.id, actor)
        assert not (agent.narrowing or {}).get("tools", {}).get("email", {}).get("limits")
