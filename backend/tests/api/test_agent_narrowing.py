"""Tests for agent narrowing override tracking.

`enforce_narrowing_logins` (agents_write.py) is the ONLY writer of
`Agent.narrowing_overridden_keys` — the column the new "N of M agents deviate"
feature will read. A tool key is only "overridden" if an operator's save
actually changed its value from what was stored before, not merely because
it appears in the payload.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.config import get_settings
from oc8.constants import ACME_TENANT_ID
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

# tests/api/<this file> -> tests -> backend -> repo root, where capas/ lives.
_PLUGINS_DIR = Path(__file__).resolve().parents[3] / "capas"


@pytest.fixture(autouse=True)
def _plugins_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("OC8_CAPAS_PATH", str(_PLUGINS_DIR))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _token(tenant: uuid.UUID) -> str:
    return get_identity_provider().mint(tenant_id=tenant, subject="u", role="org_admin")


async def test_resaving_an_unchanged_tool_does_not_mark_it_as_overridden(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        department = m.Department(
            tenant_id=tenant,
            name=f"D-{uuid.uuid4().hex}",
            frame={"tools": {"github": {"enabled": True, "read": True, "modify": False}}},
        )
        db.add(department)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=department.id,
            name="Probe",
            narrowing={"tools": {"github": {"enabled": True, "read": True, "modify": False}}},
        )
        db.add(agent)
        await db.flush()
        agent_id = agent.id

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            # Resave the narrowing with the exact same value as what's already stored.
            r = await client.put(
                f"/api/v1/agents/{agent_id}/narrowing",
                json={
                    "narrowing": {
                        "tools": {"github": {"enabled": True, "read": True, "modify": False}}
                    }
                },
                headers=headers,
            )
            assert r.status_code == 200, r.text

    async with app_session(tenant) as db:
        refreshed = await db.get(m.Agent, agent_id)
        assert refreshed.narrowing_overridden_keys == [], (
            "resending a value identical to what was already stored must not mark it as a "
            "deliberate override"
        )


async def test_resaving_a_tool_with_a_changed_value_does_mark_it_as_overridden(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        department = m.Department(
            tenant_id=tenant,
            name=f"D-{uuid.uuid4().hex}",
            frame={"tools": {"github": {"enabled": True, "read": True, "modify": True}}},
        )
        db.add(department)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=department.id, name="Probe")
        db.add(agent)
        await db.flush()
        agent_id = agent.id

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            r = await client.put(
                f"/api/v1/agents/{agent_id}/narrowing",
                json={
                    "narrowing": {
                        "tools": {"github": {"enabled": True, "read": True, "modify": False}}
                    }
                },
                headers=headers,
            )
            assert r.status_code == 200, r.text

    async with app_session(tenant) as db:
        refreshed = await db.get(m.Agent, agent_id)
        assert refreshed.narrowing_overridden_keys == ["github"]


async def test_a_realistic_frontend_shaped_resave_with_unchanged_values_is_not_marked_overridden(
    app_session: AppSessionFactory,
) -> None:
    """Regression: the department frame stores `only: None` (never set) and
    has no `connection_id` key at all (only `default_connection_id`, a
    different field with a different meaning); the frontend's `GuardrailValue`
    always sends `only: []` and a `connection_id` key (`null` when unset).
    A raw dict compare between the two never matches on shape alone, so
    EVERY save marked EVERY submitted key overridden regardless of whether
    its actual rights/approval/only VALUE differed from the frame -- this
    silently broke the "N of M agents deviate" aggregate and the agent
    Guardrails tab's own status badge for every real save from the current
    UI, which always resends every currently-relevant key this shape."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        department = m.Department(
            tenant_id=tenant,
            name=f"D-{uuid.uuid4().hex}",
            frame={
                "tools": {
                    "github": {
                        "enabled": True,
                        "read": True,
                        "modify": True,
                        "approval_eur": None,
                        "approval_actions": [],
                        "only": None,
                        "default_connection_id": None,
                    }
                }
            },
        )
        db.add(department)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=department.id, name="Probe")
        db.add(agent)
        await db.flush()
        agent_id = agent.id

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            r = await client.put(
                f"/api/v1/agents/{agent_id}/narrowing",
                json={
                    "narrowing": {
                        "tools": {
                            "github": {
                                "enabled": True,
                                "read": True,
                                "modify": True,
                                "approval_eur": None,
                                "approval_actions": [],
                                "only": [],
                                "connection_id": None,
                            }
                        }
                    }
                },
                headers=headers,
            )
            assert r.status_code == 200, r.text

    async with app_session(tenant) as db:
        refreshed = await db.get(m.Agent, agent_id)
        assert refreshed.narrowing_overridden_keys == [], (
            "the submitted rights/approval/only values are identical to the frame's own -- "
            "only the unrelated connection_id/default_connection_id key names and the "
            "only=[]-vs-None shape differ -- so this must not count as a deliberate override"
        )


async def test_setting_only_for_a_connection_with_no_value_spec_is_accepted(
    app_session: AppSessionFactory,
) -> None:
    """Regression: `only` is a plain tool-name allowlist, not a value_spec
    feature -- github_mcp/jira_mcp/microsoft365/google_workspace all ship real
    `only`-based guardrail presets (e.g. github's "Support: Issue triage, no
    code access") and none of them declare a value_spec. Gating `only` behind
    `connection_supports_value_spec` (this test's own name until this fix)
    broke every one of those presets; only `approval_eur` genuinely needs a
    value_spec, since that's where the monetary threshold gets compared
    against. Found live: applying github's own "Support" preset 422'd."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        department = m.Department(
            tenant_id=tenant,
            name=f"D-{uuid.uuid4().hex}",
            frame={"tools": {"github": {"enabled": True, "read": True, "modify": True}}},
        )
        db.add(department)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=department.id, name="Probe")
        conn = m.McpConnection(
            tenant_id=tenant,
            name="github",
            server_url="",
            transport="stdio",
            config={"_plugin_name": "github_mcp", "_connection_key": "primary"},
        )
        db.add_all([agent, conn])
        await db.flush()
        agent_id = agent.id

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            r = await client.put(
                f"/api/v1/agents/{agent_id}/narrowing",
                json={
                    "narrowing": {
                        "tools": {
                            "github": {
                                "enabled": True,
                                "read": True,
                                "modify": True,
                                "only": ["some_tool"],
                            }
                        }
                    }
                },
                headers=headers,
            )
            assert r.status_code == 200, r.text


async def test_an_empty_only_list_for_a_connection_with_no_value_spec_is_accepted(
    app_session: AppSessionFactory,
) -> None:
    """The frontend's GuardrailValue always sends `only: []` for a tool that
    never had an allowlist -- never `null` -- so this must NOT trip the same
    gate a genuinely non-empty `only` does. Regression for a live-verification
    finding: every real save from the new ToolGuardrailTable UI was 422ing on
    any connection with no value_spec, because `only` was checked with
    `is not None` instead of a real non-empty check."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        department = m.Department(
            tenant_id=tenant,
            name=f"D-{uuid.uuid4().hex}",
            frame={"tools": {"github": {"enabled": True, "read": True, "modify": True}}},
        )
        db.add(department)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=department.id, name="Probe")
        conn = m.McpConnection(
            tenant_id=tenant,
            name="github",
            server_url="",
            transport="stdio",
            config={"_plugin_name": "github_mcp", "_connection_key": "primary"},
        )
        db.add_all([agent, conn])
        await db.flush()
        agent_id = agent.id

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            r = await client.put(
                f"/api/v1/agents/{agent_id}/narrowing",
                json={
                    "narrowing": {
                        "tools": {
                            "github": {
                                "enabled": True,
                                "read": True,
                                "modify": True,
                                "only": [],
                            }
                        }
                    }
                },
                headers=headers,
            )
            assert r.status_code == 200, r.text


async def test_setting_approval_eur_for_a_connection_with_a_value_spec_is_accepted(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        department = m.Department(
            tenant_id=tenant,
            name=f"D-{uuid.uuid4().hex}",
            frame={"tools": {"odoo": {"enabled": True, "read": True, "modify": True}}},
        )
        db.add(department)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=department.id, name="Probe")
        conn = m.McpConnection(
            tenant_id=tenant,
            name="odoo",
            server_url="",
            transport="stdio",
            config={"_plugin_name": "odoo_mcp", "_connection_key": "primary"},
        )
        db.add_all([agent, conn])
        await db.flush()
        agent_id = agent.id

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            r = await client.put(
                f"/api/v1/agents/{agent_id}/narrowing",
                json={
                    "narrowing": {
                        "tools": {
                            "odoo": {
                                "enabled": True,
                                "read": True,
                                "modify": True,
                                "approval_eur": 3000,
                            }
                        }
                    }
                },
                headers=headers,
            )
            assert r.status_code == 200, r.text


async def _put_narrowing_under_a_frame_threshold(
    app_session: AppSessionFactory,
    inherited_eur: float | None,
    *,
    frame_eur: float | None = 200,
    saved_eur: float | None = None,
) -> tuple[int, str, uuid.UUID]:
    """An agent in a department whose frame puts a `frame_eur` threshold on a
    connection without a value_spec (microsoft365), saving a second tool while
    sending `inherited_eur` for the inherited one -- the payload the agent
    page's REPLACE save sends for every key it didn't edit. `saved_eur` is a
    threshold the agent's narrowing already holds from an earlier save."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    frame_policy: dict[str, object] = {"enabled": True, "read": True, "modify": True}
    if frame_eur is not None:
        frame_policy["approval_eur"] = frame_eur
    async with app_session(tenant) as db:
        department = m.Department(
            tenant_id=tenant,
            name=f"D-{uuid.uuid4().hex}",
            frame={"tools": {"microsoft365": frame_policy}},
        )
        db.add(department)
        await db.flush()
        saved: dict[str, object] = {}
        if saved_eur is not None:
            saved = {"tools": {"microsoft365": {**frame_policy, "approval_eur": saved_eur}}}
        agent = m.Agent(
            tenant_id=tenant, department_id=department.id, name="Probe", narrowing=saved
        )
        conn = m.McpConnection(
            tenant_id=tenant,
            name="microsoft365",
            server_url="",
            transport="stdio",
            config={"_plugin_name": "microsoft365", "_connection_key": "primary"},
        )
        db.add_all([agent, conn])
        await db.flush()
        agent_id = agent.id

    sent: dict[str, object] = {"enabled": True, "read": True, "modify": True}
    if inherited_eur is not None:
        sent["approval_eur"] = inherited_eur
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            r = await client.put(
                f"/api/v1/agents/{agent_id}/narrowing",
                json={
                    "narrowing": {
                        "tools": {
                            "microsoft365": sent,
                            "github": {"enabled": True, "read": True, "modify": False},
                        }
                    }
                },
                headers=headers,
            )
            return r.status_code, r.text, agent_id


async def _stored_microsoft365(app_session: AppSessionFactory, agent_id: uuid.UUID) -> dict:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        stored = agent.narrowing["tools"]["microsoft365"]
        assert isinstance(stored, dict)
        return stored


async def test_echoing_an_inherited_frame_threshold_is_accepted(
    app_session: AppSessionFactory,
) -> None:
    """Regression (E2E F3): the frame check REQUIRES the narrowing to keep the
    frame's threshold ("narrowing removes a frame approval threshold"), while
    the value_spec check rejected that very value for a connection with no
    value_spec -- so no agent in such a department could save any guardrail
    or add any tool."""
    status_code, text, agent_id = await _put_narrowing_under_a_frame_threshold(app_session, 200)
    assert status_code == 200, text
    # Not stored: the connection has no value to compare it against, and the
    # department's own threshold applies through the frame anyway.
    assert "approval_eur" not in await _stored_microsoft365(app_session, agent_id)


async def test_a_save_after_the_department_raised_its_threshold_still_works(
    app_session: AppSessionFactory,
) -> None:
    """Review of F3: the frame went from 200 to 300 after the agent's narrowing
    was saved with 200. Accepting only the frame's exact value made every save
    of that agent a 422 -- with the threshold field hidden, so nothing the
    person could change. The stored 200 never fired (no amount to compare),
    so the unchanged legacy value is dropped and the frame's 300 applies."""
    status_code, text, agent_id = await _put_narrowing_under_a_frame_threshold(
        app_session, 200, frame_eur=300, saved_eur=200
    )
    assert status_code == 200, text
    assert "approval_eur" not in await _stored_microsoft365(app_session, agent_id)
    assert '"approvalEur":300' in text.replace(" ", "")


async def test_a_new_tighter_threshold_nothing_can_enforce_is_refused_by_name(
    app_session: AppSessionFactory,
) -> None:
    """The connection reports no amount, so a positive threshold never fires:
    stored, it would be shown as a guardrail that never holds; dropped, the
    person would never learn why. Refused, saying what makes sense."""
    status_code, text, _agent = await _put_narrowing_under_a_frame_threshold(app_session, 50)
    assert status_code == 422, text
    assert "value_spec_not_supported" in text
    assert "only 0" in text


async def test_a_zero_threshold_is_kept_because_it_is_enforced(
    app_session: AppSessionFactory,
) -> None:
    """Security review: a missing amount counts as zero, so €0 asks for every
    modify even on a connection without a value spec. Dropping it would
    loosen the agent's guardrail to the department's."""
    status_code, text, agent_id = await _put_narrowing_under_a_frame_threshold(app_session, 0)
    assert status_code == 200, text
    assert (await _stored_microsoft365(app_session, agent_id))["approval_eur"] == 0


async def test_a_stored_zero_threshold_sent_back_unchanged_is_kept(
    app_session: AppSessionFactory,
) -> None:
    status_code, text, agent_id = await _put_narrowing_under_a_frame_threshold(
        app_session, 0, frame_eur=300, saved_eur=0
    )
    assert status_code == 200, text
    assert (await _stored_microsoft365(app_session, agent_id))["approval_eur"] == 0


async def test_a_zero_threshold_where_the_department_sets_none_is_kept(
    app_session: AppSessionFactory,
) -> None:
    status_code, text, agent_id = await _put_narrowing_under_a_frame_threshold(
        app_session, 0, frame_eur=None
    )
    assert status_code == 200, text
    assert (await _stored_microsoft365(app_session, agent_id))["approval_eur"] == 0


async def test_leaving_out_an_inherited_frame_threshold_is_accepted(
    app_session: AppSessionFactory,
) -> None:
    """The page no longer sends a threshold for a connection without a value
    spec; the frame's still applies, so leaving it out removes nothing."""
    status_code, text, _agent = await _put_narrowing_under_a_frame_threshold(app_session, None)
    assert status_code == 200, text
    assert '"approvalEur":200' in text.replace(" ", "")


@pytest.mark.parametrize("inherited_eur", [200, None])
async def test_an_unchanged_inherited_tool_is_not_marked_narrowed(
    app_session: AppSessionFactory, inherited_eur: float | None
) -> None:
    """E2E N6: saving another tool's guardrail stored microsoft365 without the
    department's €200 (nothing there can compare an amount), and the badge
    then said "Narrowed". What the agent may do there is exactly the
    department's: compared as the effective policy, nothing diverged."""
    status_code, text, agent_id = await _put_narrowing_under_a_frame_threshold(
        app_session, inherited_eur
    )
    assert status_code == 200, text
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        assert "microsoft365" not in (agent.narrowing_overridden_keys or [])


async def test_loosening_an_inherited_frame_threshold_is_still_rejected(
    app_session: AppSessionFactory,
) -> None:
    status_code, text, _agent = await _put_narrowing_under_a_frame_threshold(app_session, 500)
    assert status_code == 422, text
    assert "narrowing_exceeds_frame" in text


async def test_an_agent_own_threshold_where_the_department_sets_none_is_refused(
    app_session: AppSessionFactory,
) -> None:
    """A positive threshold the department never set, on a connection with
    nothing to compare it against, would be a guardrail that never holds."""
    status_code, text, _agent = await _put_narrowing_under_a_frame_threshold(
        app_session, 100, frame_eur=None
    )
    assert status_code == 422, text
    assert "value_spec_not_supported" in text


async def test_resetting_an_overridden_key_drops_it_and_clears_the_override_flag(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        department = m.Department(
            tenant_id=tenant,
            name=f"D-{uuid.uuid4().hex}",
            frame={"tools": {"github": {"enabled": True, "read": True, "modify": False}}},
        )
        db.add(department)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=department.id,
            name="Probe",
            narrowing={"tools": {"github": {"enabled": True, "read": True, "modify": True}}},
            narrowing_overridden_keys=["github"],
        )
        db.add(agent)
        await db.flush()
        agent_id = agent.id

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            r = await client.post(
                f"/api/v1/agents/{agent_id}/narrowing/github/reset", headers=headers
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert "github" not in body["narrowingTools"]
            assert body["narrowingOverriddenKeys"] == []
            # Falls back to whatever the department frame currently grants.
            assert body["effectiveTools"]["github"]["modify"] is False
            assert body["toolPolicySources"]["github"] == "department"

    async with app_session(tenant) as db:
        refreshed = await db.get(m.Agent, agent_id)
        assert refreshed is not None
        assert "github" not in refreshed.narrowing.get("tools", {})
        assert refreshed.narrowing_overridden_keys == []


async def test_resetting_an_untracked_key_404s(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        department = m.Department(
            tenant_id=tenant,
            name=f"D-{uuid.uuid4().hex}",
            frame={"tools": {"github": {"enabled": True, "read": True, "modify": False}}},
        )
        db.add(department)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=department.id, name="Probe")
        db.add(agent)
        await db.flush()
        agent_id = agent.id

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            r = await client.post(
                f"/api/v1/agents/{agent_id}/narrowing/github/reset", headers=headers
            )
            assert r.status_code == 404, r.text


async def _hire_under_a_frame_threshold(
    app_session: AppSessionFactory, sent_eur: float | None
) -> tuple[int, str]:
    """Hiring into a department with a €200 threshold on microsoft365 (no
    value_spec), granting it with `sent_eur` -- the hire wizard no longer sends
    the department's threshold for such a connection."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    policy: dict[str, object] = {"enabled": True, "read": True, "modify": True}
    async with app_session(tenant) as db:
        department = m.Department(
            tenant_id=tenant,
            name=f"D-{uuid.uuid4().hex}",
            frame={"tools": {"microsoft365": {**policy, "approval_eur": 200}}},
        )
        db.add(department)
        db.add(
            m.McpConnection(
                tenant_id=tenant,
                name="microsoft365",
                server_url="",
                transport="stdio",
                config={"_plugin_name": "microsoft365", "_connection_key": "primary"},
            )
        )
        await db.flush()
        department_id = department.id

    sent = dict(policy)
    if sent_eur is not None:
        sent["approval_eur"] = sent_eur
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.post(
                "/api/v1/agents",
                json={
                    "departmentId": str(department_id),
                    "name": f"A-{uuid.uuid4().hex[:8]}",
                    "roleTitle": "Tester",
                    "mission": "m",
                    "narrowing": {"tools": {"microsoft365": sent}},
                },
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
            return r.status_code, r.text


async def test_hiring_without_the_department_threshold_on_a_connection_without_value_spec(
    app_session: AppSessionFactory,
) -> None:
    status_code, text = await _hire_under_a_frame_threshold(app_session, None)
    assert status_code == 201, text


async def test_hiring_with_a_looser_threshold_than_the_department_is_rejected(
    app_session: AppSessionFactory,
) -> None:
    status_code, text = await _hire_under_a_frame_threshold(app_session, 500)
    assert status_code == 422, text
    assert "narrowing_exceeds_frame" in text


async def test_hiring_with_a_zero_threshold_keeps_it(app_session: AppSessionFactory) -> None:
    import json

    status_code, text = await _hire_under_a_frame_threshold(app_session, 0)
    assert status_code == 201, text
    agent_id = uuid.UUID(json.loads(text)["id"])
    assert (await _stored_microsoft365(app_session, agent_id))["approval_eur"] == 0


async def test_hiring_with_a_new_tighter_threshold_nothing_can_enforce_is_refused(
    app_session: AppSessionFactory,
) -> None:
    status_code, text = await _hire_under_a_frame_threshold(app_session, 50)
    assert status_code == 422, text
    assert "value_spec_not_supported" in text
