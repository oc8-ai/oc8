"""agent.narrowing.set / agent.narrowing.reset -- mirrors PUT /agents/{id}/
narrowing and POST /agents/{id}/narrowing/{key}/reset exactly, through
apply_operation instead of the REST route."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from oc8 import models as m
from oc8.agents.versioning import draft_status, publish_version, version_payload
from oc8.auth import Principal
from oc8.copilot.proposals import ProposalRejected, apply_proposal, create_proposal

pytestmark = pytest.mark.asyncio

_CAPAS_DIR = str(Path(__file__).resolve().parents[3] / "capas")


def _actor(tenant: uuid.UUID) -> Principal:
    return Principal(subject="operator-1", tenant_id=tenant, role="org_admin")


async def test_narrowing_set_rejects_a_grant_the_department_frame_does_not_allow(
    app_session, acme_tenant
) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        # `narrowing_within_frame` only flags a KEY THE FRAME ALREADY GRANTS
        # being widened beyond what the frame allows for it -- a key entirely
        # absent from the frame is an agent-exclusive grant, not a violation
        # (see `pdp.narrowing_within_frame`'s own docstring and
        # `tests/authz/test_pdp.py`'s
        # `test_narrowing_within_frame_does_not_flag_a_tool_absent_from_the_frame`).
        # So the frame here grants `github_mcp` with `modify: False`, and the
        # proposed narrowing asks for `modify: True` -- a real widening.
        dept = m.Department(
            tenant_id=acme_tenant,
            name="Sales",
            frame={"tools": {"github_mcp": {"enabled": True, "read": True, "modify": False}}},
        )
        db.add(dept)
        await db.flush()
        # A name distinct from other copilot tests' "Nora" -- `app_session`
        # commits, and several other files (`test_create_operations.py`,
        # `test_department_operations.py`, `test_proposal_listing.py`) look
        # up an agent by `name == "Nora"` in this same session-scoped
        # `acme_tenant`; reusing that name here would make one of those
        # lookups raise `MultipleResultsFound` depending on file/run order.
        agent = m.Agent(tenant_id=acme_tenant, department_id=dept.id, name="Petra", definition={})
        db.add(agent)
        await db.flush()

        proposal = await create_proposal(
            db,
            actor,
            [
                {
                    "type": "agent.narrowing.set",
                    "agentId": str(agent.id),
                    "narrowing": {
                        "tools": {"github_mcp": {"enabled": True, "read": True, "modify": True}}
                    },
                }
            ],
        )
        # narrowing_within_frame must reject this -- same violation
        # set/narrowing's own REST route produces, surfaced here as
        # InvalidOperation -> proposal "rejected".
        with pytest.raises(ProposalRejected):
            await apply_proposal(db, proposal.id, actor)


async def test_narrowing_reset_removes_one_key_and_its_override_marker(
    app_session, acme_tenant
) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        dept = m.Department(tenant_id=acme_tenant, name="Sales", frame={"tools": {}})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=acme_tenant,
            department_id=dept.id,
            name="Petra",
            definition={},
            narrowing={"tools": {"github_mcp": {"enabled": False}}},
            narrowing_overridden_keys=["github_mcp"],
        )
        db.add(agent)
        await db.flush()

        proposal = await create_proposal(
            db,
            actor,
            [
                {
                    "type": "agent.narrowing.reset",
                    "agentId": str(agent.id),
                    "connectionName": "github_mcp",
                }
            ],
        )
        result = await apply_proposal(db, proposal.id, actor)
        assert result.status == "applied"

        await db.refresh(agent)
        assert "github_mcp" not in agent.narrowing.get("tools", {})
        assert "github_mcp" not in (agent.narrowing_overridden_keys or [])


async def test_narrowing_reset_rejects_a_key_not_currently_set(app_session, acme_tenant) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        dept = m.Department(tenant_id=acme_tenant, name="Sales", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=acme_tenant, department_id=dept.id, name="Petra", definition={})
        db.add(agent)
        await db.flush()

        proposal = await create_proposal(
            db,
            actor,
            [
                {
                    "type": "agent.narrowing.reset",
                    "agentId": str(agent.id),
                    "connectionName": "github_mcp",
                }
            ],
        )
        with pytest.raises(ProposalRejected):
            await apply_proposal(db, proposal.id, actor)


async def test_narrowing_set_on_a_published_agent_is_published(app_session, acme_tenant) -> None:
    """Runs execute the published version: an approved tool-access change on
    an agent with no draft goes live, override marker included. It used to
    stay a draft because the override marker (a versioned column) was written
    before the draft check."""
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        dept = m.Department(tenant_id=acme_tenant, name="Sales", frame={"tools": {}})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=acme_tenant, department_id=dept.id, name="Petra", definition={})
        db.add(agent)
        await db.flush()
        await publish_version(db, agent)
        narrowing = {"tools": {"odoo": {"enabled": True, "read": True, "modify": False}}}
        proposal = await create_proposal(
            db,
            actor,
            [{"type": "agent.narrowing.set", "agentId": str(agent.id), "narrowing": narrowing}],
        )
        result = await apply_proposal(db, proposal.id, actor)
        assert result.status == "applied"

        await db.refresh(agent)
        assert agent.narrowing_overridden_keys == ["odoo"]
        assert not (await draft_status(db, agent)).dirty
        live = await db.get(m.AgentVersion, agent.current_version_id)
        assert live is not None and live.version_no == 2
        assert version_payload(live)["narrowing"] == narrowing


async def _propose_threshold_after_a_frame_raise(
    app_session, acme_tenant, sent_eur: float
) -> tuple[m.Agent, object]:
    """Review of F3, through the Copilot: the department raised its threshold
    on a connection without a value spec (microsoft365) from 200 to 300 after
    the agent's narrowing was saved with 200, and the Copilot proposes the
    agent's narrowing again with `sent_eur`."""
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        policy = {"enabled": True, "read": True, "modify": True}
        dept = m.Department(
            tenant_id=acme_tenant,
            name="Sales",
            frame={"tools": {"microsoft365": {**policy, "approval_eur": 300}}},
        )
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=acme_tenant,
            department_id=dept.id,
            name="Petra",
            definition={},
            narrowing={"tools": {"microsoft365": {**policy, "approval_eur": 200}}},
        )
        conn = m.McpConnection(
            tenant_id=acme_tenant,
            name="microsoft365",
            server_url="",
            transport="stdio",
            config={"_plugin_name": "microsoft365", "_connection_key": "primary"},
        )
        db.add_all([agent, conn])
        await db.flush()
        proposal = await create_proposal(
            db,
            actor,
            [
                {
                    "type": "agent.narrowing.set",
                    "agentId": str(agent.id),
                    "narrowing": {"tools": {"microsoft365": {**policy, "approval_eur": sent_eur}}},
                }
            ],
        )
        try:
            result: object = await apply_proposal(db, proposal.id, actor)
        except ProposalRejected as exc:
            result = exc
        await db.refresh(agent)
        return agent, result


async def test_narrowing_set_after_the_department_raised_its_threshold_is_applied(
    app_session, acme_tenant
) -> None:
    agent, result = await _propose_threshold_after_a_frame_raise(app_session, acme_tenant, 200)
    assert not isinstance(result, ProposalRejected), result
    assert "approval_eur" not in agent.narrowing["tools"]["microsoft365"]


async def test_narrowing_set_with_a_looser_threshold_than_the_frame_is_rejected(
    app_session, acme_tenant
) -> None:
    _agent, result = await _propose_threshold_after_a_frame_raise(app_session, acme_tenant, 500)
    assert isinstance(result, ProposalRejected)


async def test_a_guardrail_edit_on_a_narrowing_without_the_frame_threshold_is_applied(
    app_session, acme_tenant, monkeypatch
) -> None:
    """A saved narrowing no longer carries the department's threshold on a
    connection without a value spec, so the Copilot's guardrail edit must not
    read its absence as removing it."""
    from oc8.config import get_settings

    monkeypatch.setenv("OC8_CAPAS_PATH", _CAPAS_DIR)
    get_settings.cache_clear()
    actor = _actor(acme_tenant)
    try:
        async with app_session(acme_tenant) as db:
            policy = {"enabled": True, "read": True, "modify": True}
            dept = m.Department(
                tenant_id=acme_tenant,
                name="Sales",
                frame={"tools": {"microsoft365": {**policy, "approval_eur": 200}}},
            )
            db.add(dept)
            await db.flush()
            agent = m.Agent(
                tenant_id=acme_tenant,
                department_id=dept.id,
                name="Petra",
                definition={},
                narrowing={"tools": {"microsoft365": dict(policy)}},
            )
            conn = m.McpConnection(
                tenant_id=acme_tenant,
                name="microsoft365",
                server_url="",
                transport="stdio",
                config={"_plugin_name": "microsoft365", "_connection_key": "primary"},
            )
            db.add_all([agent, conn])
            await db.flush()
            proposal = await create_proposal(
                db,
                actor,
                [
                    {
                        "type": "agent.guardrail.set",
                        "agentId": str(agent.id),
                        "connectionName": "microsoft365",
                        "function": "mail_delete",
                        "decision": "not_allowed",
                    }
                ],
            )
            result = await apply_proposal(db, proposal.id, actor)
            assert result.status == "applied"
    finally:
        get_settings.cache_clear()


async def test_narrowing_set_with_a_new_tighter_threshold_nothing_can_enforce_is_rejected(
    app_session, acme_tenant
) -> None:
    """Stored 200, frame 300: a NEW 100 never fires on a connection without a
    value spec -- refused, never dropped silently."""
    _agent, result = await _propose_threshold_after_a_frame_raise(app_session, acme_tenant, 100)
    assert isinstance(result, ProposalRejected)


async def test_narrowing_set_keeps_a_zero_threshold(app_session, acme_tenant) -> None:
    """A missing amount counts as zero, so €0 is enforced: kept."""
    agent, result = await _propose_threshold_after_a_frame_raise(app_session, acme_tenant, 0)
    assert not isinstance(result, ProposalRejected), result
    assert agent.narrowing["tools"]["microsoft365"]["approval_eur"] == 0


async def test_a_copilot_change_to_a_pending_hire_is_held_as_a_draft(
    app_session, acme_tenant
) -> None:
    """Review of round 7: a Copilot change published over a hire that awaits
    approval. It is held in the working copy; the published v1 -- what the
    approver approves -- stays as it was."""
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        dept = m.Department(tenant_id=acme_tenant, name="Sales", frame={"tools": {}})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=acme_tenant, department_id=dept.id, name="Pia", definition={},
            status="pending_approval",
        )
        db.add(agent)
        await db.flush()
        await publish_version(db, agent, at_hire=True)
        narrowing = {"tools": {"odoo": {"enabled": True, "read": True, "modify": False}}}
        proposal = await create_proposal(
            db,
            actor,
            [{"type": "agent.narrowing.set", "agentId": str(agent.id), "narrowing": narrowing}],
        )
        assert (await apply_proposal(db, proposal.id, actor)).status == "applied"
        await db.refresh(agent)
        live = await db.get(m.AgentVersion, agent.current_version_id)
        assert live is not None and live.version_no == 1
        assert (await draft_status(db, agent)).dirty
