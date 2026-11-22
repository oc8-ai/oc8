"""Editing a shipped or store skill in place, keeping the edit across shipped
updates, and resetting it to the shipped version.

A skill a capa ships used to be read-only: the only way to change it was to
duplicate it, which cut the copy off from every later fix the capa shipped.
Now an admin edits it where it is. The row keeps its origin and its link to the
shipped content (`shipped_version_id`), `customized_at` records that it was
changed, and a later shipped update never overwrites the edit -- it is kept
beside it, and `shipped_update_at` says one is waiting.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.capas.lifecycle import enable_plugin
from oc8.capas.service import install_plugin
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

SKILL_MANIFEST: dict[str, Any] = {
    "name": "acme.invoice-skill",
    "version": "1.0.0",
    "type": "skill",
    "trust": "first_party",
    "skill_template": {
        "name": "Invoice check",
        "description": "Checks invoices against the PO.",
        "category": "finance",
        "instruction": "Compare the invoice total to the purchase order.",
        "requires_tools": [],
        "guardrails": ["Never approve above 10k without a human."],
    },
}


def _h(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    tok = get_identity_provider().mint(tenant_id=tenant, subject="author", role=role)
    return {"Authorization": f"Bearer {tok}"}


def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def _upgraded(version: str, instruction: str) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads(json.dumps(SKILL_MANIFEST))
    manifest["version"] = version
    manifest["skill_template"]["instruction"] = instruction
    return manifest


async def _ship(session: AppSessionFactory, tenant: uuid.UUID, manifest: dict[str, Any]) -> None:
    """Install (or upgrade to) `manifest` and enable it -- what a capa update does."""
    async with session(tenant) as db:
        version = await install_plugin(db, tenant_id=tenant, manifest_data=manifest)
        await enable_plugin(
            db,
            tenant_id=tenant,
            capa_id=version.capa_id,
            granted_permissions=list(version.permissions),
        )


async def _skill(session: AppSessionFactory, tenant: uuid.UUID) -> m.Skill:
    async with session(tenant) as db:
        return (
            await db.execute(select(m.Skill).where(m.Skill.name == "Invoice check"))
        ).scalar_one()


async def _instruction(session: AppSessionFactory, tenant: uuid.UUID) -> str:
    async with session(tenant) as db:
        skill = (
            await db.execute(select(m.Skill).where(m.Skill.name == "Invoice check"))
        ).scalar_one()
        assert skill.current_version_id is not None
        version = await db.get(m.SkillVersion, skill.current_version_id)
        assert version is not None
        return str(version.definition["instruction"])


async def _edit(c: AsyncClient, tenant: uuid.UUID, skill_id: uuid.UUID, text: str) -> Any:
    r = await c.post(
        f"/api/v1/skills/{skill_id}/versions",
        json={"instructions": text, "guardrails": ["Ours."]},
        headers=_h(tenant),
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- materialise


async def test_a_shipped_skill_records_the_version_it_shipped(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    await _ship(app_session, tenant, SKILL_MANIFEST)
    skill = await _skill(app_session, tenant)
    assert skill.shipped_version_id == skill.current_version_id
    assert skill.customized_at is None
    assert skill.shipped_update_at is None


async def test_an_uncustomised_shipped_skill_follows_a_shipped_update(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    await _ship(app_session, tenant, SKILL_MANIFEST)
    await _ship(app_session, tenant, _upgraded("1.1.0", "Check the total and the VAT id."))

    assert await _instruction(app_session, tenant) == "Check the total and the VAT id."
    skill = await _skill(app_session, tenant)
    assert skill.shipped_version_id == skill.current_version_id
    assert skill.shipped_update_at is None
    async with app_session(tenant) as db:
        version = await db.get(m.SkillVersion, skill.current_version_id)
        assert version is not None and version.semver == "1.1.0"


async def test_a_capa_release_that_leaves_the_skill_unchanged_adds_no_version(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    await _ship(app_session, tenant, SKILL_MANIFEST)
    same = _upgraded("1.0.1", SKILL_MANIFEST["skill_template"]["instruction"])
    await _ship(app_session, tenant, same)
    async with app_session(tenant) as db:
        count = (await db.execute(select(func.count()).select_from(m.SkillVersion))).scalar_one()
        assert count == 1


async def test_a_local_skill_of_the_same_name_is_never_overwritten(
    app_session: AppSessionFactory,
) -> None:
    """The name match only finds the capa's own skill: one an admin authored
    under the same name was never shipped and must stay as it is."""
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        r = await c.post(
            "/api/v1/skills",
            json={"name": "Invoice check", "instructions": "Our own text."},
            headers=_h(tenant),
        )
        assert r.status_code == 201, r.text
    await _ship(app_session, tenant, SKILL_MANIFEST)
    await _ship(app_session, tenant, _upgraded("1.1.0", "Shipped text."))
    assert await _instruction(app_session, tenant) == "Our own text."
    skill = await _skill(app_session, tenant)
    assert skill.origin == "local"
    assert skill.shipped_version_id is None


# ---------------------------------------------------------------- edit


async def test_editing_a_store_skill_keeps_its_origin_and_marks_it_customised(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    await _ship(app_session, tenant, SKILL_MANIFEST)
    shipped = await _skill(app_session, tenant)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        got = await _edit(c, tenant, shipped.id, "Our way of checking invoices.")
        assert got["origin"] == "store"
        assert got["customizedAt"]
        assert got["shippedUpdateAvailable"] is False
        listed = next(
            s
            for s in (await c.get("/api/v1/skills", headers=_h(tenant))).json()["items"]
            if s["id"] == str(shipped.id)
        )
        assert listed["customizedAt"]
        assert listed["instructions"] == "Our way of checking invoices."

    skill = await _skill(app_session, tenant)
    assert skill.origin == "store"
    assert skill.author == "acme.invoice-skill"
    assert skill.customized_at is not None
    # The shipped content is kept for a reset.
    assert skill.shipped_version_id == shipped.current_version_id
    assert skill.current_version_id != shipped.current_version_id


async def test_editing_a_local_skill_does_not_mark_it_customised() -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        sid = (
            await c.post(
                "/api/v1/skills",
                json={"name": "Mine", "instructions": "Do it."},
                headers=_h(tenant),
            )
        ).json()["id"]
        got = await _edit(c, tenant, uuid.UUID(sid), "Do it better.")
        assert got["customizedAt"] is None


async def test_patching_a_store_skills_instructions_marks_it_customised(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    await _ship(app_session, tenant, SKILL_MANIFEST)
    shipped = await _skill(app_session, tenant)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        r = await c.patch(
            f"/api/v1/skills/{shipped.id}",
            json={"description": "Checks invoices against the PO."},
            headers=_h(tenant),
        )
        assert r.status_code == 200, r.text
        # The name is not shipped content: it is how the shipped skill is found.
        assert r.json()["customizedAt"] is None
        r = await c.patch(
            f"/api/v1/skills/{shipped.id}",
            json={"instructions": "Patched in place."},
            headers=_h(tenant),
        )
        assert r.status_code == 200, r.text
        assert r.json()["customizedAt"]
    skill = await _skill(app_session, tenant)
    assert skill.customized_at is not None
    assert await _instruction(app_session, tenant) == "Patched in place."
    # Copy on write: the shipped version itself is never changed.
    async with app_session(tenant) as db:
        kept = await db.get(m.SkillVersion, skill.shipped_version_id)
        assert kept is not None
        assert kept.definition["instruction"].startswith("Compare the invoice")


async def test_a_shipped_update_leaves_a_customised_skill_alone_but_says_so(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    await _ship(app_session, tenant, SKILL_MANIFEST)
    shipped = await _skill(app_session, tenant)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        edited = await _edit(c, tenant, shipped.id, "Our way of checking invoices.")

        await _ship(app_session, tenant, _upgraded("1.1.0", "Check the total and the VAT id."))

        assert await _instruction(app_session, tenant) == "Our way of checking invoices."
        skill = await _skill(app_session, tenant)
        assert str(skill.current_version_id) == edited["currentVersionId"]
        assert skill.customized_at is not None
        assert skill.shipped_update_at is not None
        assert skill.shipped_version_id not in (shipped.current_version_id, None)
        async with app_session(tenant) as db:
            newer = await db.get(m.SkillVersion, skill.shipped_version_id)
            assert newer is not None
            assert newer.semver == "1.1.0"
            assert newer.definition["instruction"] == "Check the total and the VAT id."

        listed = next(
            s
            for s in (await c.get("/api/v1/skills", headers=_h(tenant))).json()["items"]
            if s["id"] == str(shipped.id)
        )
        assert listed["shippedUpdateAvailable"] is True


async def test_a_skill_edited_before_tracking_existed_is_still_left_alone(
    app_session: AppSessionFactory,
) -> None:
    """A store skill whose current version is no longer the shipped one was
    changed by someone, flag or not -- the shipped update must not replace it."""
    tenant = uuid.uuid4()
    await _ship(app_session, tenant, SKILL_MANIFEST)
    shipped = await _skill(app_session, tenant)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        await _edit(c, tenant, shipped.id, "Our way of checking invoices.")
    async with app_session(tenant) as db:
        row = await db.get(m.Skill, shipped.id)
        assert row is not None
        row.customized_at = None
    await _ship(app_session, tenant, _upgraded("1.1.0", "Check the total and the VAT id."))
    assert await _instruction(app_session, tenant) == "Our way of checking invoices."


# ---------------------------------------------------------------- reset


async def test_reset_restores_the_shipped_content_and_clears_the_flag(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    await _ship(app_session, tenant, SKILL_MANIFEST)
    shipped = await _skill(app_session, tenant)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        await _edit(c, tenant, shipped.id, "Our way of checking invoices.")
        r = await c.post(f"/api/v1/skills/{shipped.id}/reset", json={}, headers=_h(tenant))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["customizedAt"] is None
        assert body["shippedUpdateAvailable"] is False
        assert body["instructions"] == "Compare the invoice total to the purchase order."
        assert body["guardrails"] == ["Never approve above 10k without a human."]
        assert body["currentVersionId"] == str(shipped.current_version_id)

    skill = await _skill(app_session, tenant)
    assert skill.customized_at is None
    assert skill.current_version_id == skill.shipped_version_id


async def test_reset_after_a_shipped_update_takes_the_newer_shipped_version(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    await _ship(app_session, tenant, SKILL_MANIFEST)
    shipped = await _skill(app_session, tenant)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        await _edit(c, tenant, shipped.id, "Our way of checking invoices.")
        await _ship(app_session, tenant, _upgraded("1.1.0", "Check the total and the VAT id."))
        r = await c.post(f"/api/v1/skills/{shipped.id}/reset", json={}, headers=_h(tenant))
        assert r.status_code == 200, r.text
        assert r.json()["instructions"] == "Check the total and the VAT id."
        assert r.json()["version"] == "1.1.0"
        assert r.json()["shippedUpdateAvailable"] is False

    # Back on the shipped track: the next update applies again.
    await _ship(app_session, tenant, _upgraded("1.2.0", "Newest shipped text."))
    assert await _instruction(app_session, tenant) == "Newest shipped text."


async def test_reset_can_move_assignments_to_the_shipped_version(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    await _ship(app_session, tenant, SKILL_MANIFEST)
    shipped = await _skill(app_session, tenant)
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Finance", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Clerk",
            status="stopped",
            narrowing={},
            definition={},
        )
        db.add(agent)
        await db.flush()
        agent_id = agent.id
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        edited = await _edit(c, tenant, shipped.id, "Our way of checking invoices.")
        r = await c.post(
            f"/api/v1/agents/{agent_id}/skills",
            json={"skillVersionId": edited["currentVersionId"]},
            headers=_h(tenant),
        )
        assert r.status_code == 201, r.text
        r = await c.post(
            f"/api/v1/skills/{shipped.id}/reset",
            json={"updateAssignments": True},
            headers=_h(tenant),
        )
        assert r.status_code == 200, r.text
        assert r.json()["assignmentsUpdated"] == 1
    async with app_session(tenant) as db:
        pinned = (
            await db.execute(
                select(m.SkillAssignment.skill_version_id).where(
                    m.SkillAssignment.agent_id == agent_id
                )
            )
        ).scalar_one()
        assert pinned == shipped.current_version_id


async def test_reset_refuses_a_local_skill() -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        sid = (
            await c.post(
                "/api/v1/skills",
                json={"name": "Mine", "instructions": "Do it."},
                headers=_h(tenant),
            )
        ).json()["id"]
        r = await c.post(f"/api/v1/skills/{sid}/reset", json={}, headers=_h(tenant))
        assert r.status_code == 409, r.text


async def test_reset_requires_skill_manage(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    await _ship(app_session, tenant, SKILL_MANIFEST)
    shipped = await _skill(app_session, tenant)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        r = await c.post(
            f"/api/v1/skills/{shipped.id}/reset", json={}, headers=_h(tenant, "member")
        )
        assert r.status_code == 403


async def test_the_first_edit_of_a_store_skill_without_a_record_keeps_the_shipped_one(
    app_session: AppSessionFactory,
) -> None:
    """A store skill that predates the shipped-version record (seeded, or
    imported before it existed) gets one at its first edit: the version it had
    until then is what a reset goes back to."""
    tenant = uuid.uuid4()
    await _ship(app_session, tenant, SKILL_MANIFEST)
    async with app_session(tenant) as db:
        row = (
            await db.execute(select(m.Skill).where(m.Skill.name == "Invoice check"))
        ).scalar_one()
        row.shipped_version_id = None
        before = row.current_version_id
        skill_id = row.id
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        await _edit(c, tenant, skill_id, "Ours.")
        r = await c.post(f"/api/v1/skills/{skill_id}/reset", json={}, headers=_h(tenant))
        assert r.status_code == 200, r.text
        assert r.json()["currentVersionId"] == str(before)


async def test_reset_of_another_tenants_skill_is_a_404(app_session: AppSessionFactory) -> None:
    owner, other = uuid.uuid4(), uuid.uuid4()
    await _ship(app_session, owner, SKILL_MANIFEST)
    shipped = await _skill(app_session, owner)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        await _edit(c, owner, shipped.id, "Ours.")
        r = await c.post(f"/api/v1/skills/{shipped.id}/reset", json={}, headers=_h(other))
        assert r.status_code == 404, r.text
    skill = await _skill(app_session, owner)
    assert skill.customized_at is not None


async def test_the_dto_says_whether_a_skill_is_customised(app_session: AppSessionFactory) -> None:
    """`customized` is computed like the update path decides: flagged, or simply
    no longer on the shipped version."""
    tenant = uuid.uuid4()
    await _ship(app_session, tenant, SKILL_MANIFEST)
    shipped = await _skill(app_session, tenant)
    app = create_app()

    async def listed(c: AsyncClient) -> Any:
        rows = (await c.get("/api/v1/skills", headers=_h(tenant))).json()["items"]
        return next(s for s in rows if s["id"] == str(shipped.id))

    async with LifespanManager(app), _client(app) as c:
        assert (await listed(c))["customized"] is False
        await _edit(c, tenant, shipped.id, "Ours.")
        assert (await listed(c))["customized"] is True
        async with app_session(tenant) as db:
            row = await db.get(m.Skill, shipped.id)
            assert row is not None
            row.customized_at = None
        assert (await listed(c))["customized"] is True


async def test_publishing_the_current_content_again_is_a_no_op(
    app_session: AppSessionFactory,
) -> None:
    """Saving a shipped skill without changing its content must not mark it
    customised -- or it would stop following shipped updates for nothing."""
    tenant = uuid.uuid4()
    await _ship(app_session, tenant, SKILL_MANIFEST)
    shipped = await _skill(app_session, tenant)
    spec = SKILL_MANIFEST["skill_template"]
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        r = await c.post(
            f"/api/v1/skills/{shipped.id}/versions",
            json={
                "instructions": spec["instruction"],
                "tools": spec["requires_tools"],
                "knowledge": [],
                "guardrails": spec["guardrails"],
            },
            headers=_h(tenant),
        )
        assert r.status_code == 200, r.text
        assert r.json()["currentVersionId"] == str(shipped.current_version_id)
        assert r.json()["customized"] is False
    skill = await _skill(app_session, tenant)
    assert skill.customized_at is None
    assert skill.current_version_id == shipped.current_version_id
    async with app_session(tenant) as db:
        count = (await db.execute(select(func.count()).select_from(m.SkillVersion))).scalar_one()
        assert count == 1


async def test_a_shipped_update_syncs_description_and_category_of_an_uncustomised_skill(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    await _ship(app_session, tenant, SKILL_MANIFEST)
    upgraded = _upgraded("1.1.0", "Check the total and the VAT id.")
    upgraded["skill_template"]["description"] = "Checks invoices and VAT ids."
    upgraded["skill_template"]["category"] = "accounting"
    await _ship(app_session, tenant, upgraded)
    skill = await _skill(app_session, tenant)
    assert skill.description == "Checks invoices and VAT ids."
    assert skill.category == "accounting"
    assert skill.name == "Invoice check"


async def test_a_shipped_update_leaves_a_customised_skills_description_alone(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    await _ship(app_session, tenant, SKILL_MANIFEST)
    shipped = await _skill(app_session, tenant)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        await _edit(c, tenant, shipped.id, "Ours.")
    upgraded = _upgraded("1.1.0", "Check the total and the VAT id.")
    upgraded["skill_template"]["description"] = "Checks invoices and VAT ids."
    await _ship(app_session, tenant, upgraded)
    skill = await _skill(app_session, tenant)
    assert skill.description == "Checks invoices against the PO."


async def test_skill_versions_are_hashed_in_one_place() -> None:
    from oc8.api.v1 import skills_write
    from oc8.capas import materialise
    from oc8.skills import shipped

    assert not hasattr(skills_write, "_artifact_hash")
    assert not hasattr(materialise, "_hash")
    assert vars(skills_write)["artifact_hash"] is shipped.artifact_hash
    assert vars(materialise)["artifact_hash"] is shipped.artifact_hash


async def _patch(c: AsyncClient, tenant: uuid.UUID, skill_id: uuid.UUID, **body: Any) -> Any:
    r = await c.patch(f"/api/v1/skills/{skill_id}", json=body, headers=_h(tenant))
    assert r.status_code == 200, r.text
    return r.json()


async def test_editing_a_shipped_skills_description_customises_it_and_survives_a_re_enable(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    await _ship(app_session, tenant, SKILL_MANIFEST)
    shipped = await _skill(app_session, tenant)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        got = await _patch(c, tenant, shipped.id, description="Our description.")
        assert got["customized"] is True
    # Same manifest again: a re-enable must not write the shipped text back.
    await _ship(
        app_session, tenant, _upgraded("1.0.1", SKILL_MANIFEST["skill_template"]["instruction"])
    )
    skill = await _skill(app_session, tenant)
    assert skill.description == "Our description."
    assert skill.customized_at is not None
    assert skill.shipped_update_at is None


async def test_editing_a_shipped_skills_category_customises_it_and_survives_a_re_enable(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    await _ship(app_session, tenant, SKILL_MANIFEST)
    shipped = await _skill(app_session, tenant)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        got = await _patch(c, tenant, shipped.id, category="operations")
        assert got["customized"] is True
    await _ship(
        app_session, tenant, _upgraded("1.0.1", SKILL_MANIFEST["skill_template"]["instruction"])
    )
    skill = await _skill(app_session, tenant)
    assert skill.category == "operations"
    assert skill.customized_at is not None


async def test_a_newer_shipped_description_on_a_customised_skill_is_recorded_not_applied(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    await _ship(app_session, tenant, SKILL_MANIFEST)
    shipped = await _skill(app_session, tenant)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        await _patch(c, tenant, shipped.id, description="Our description.")
    upgraded = _upgraded("1.0.1", SKILL_MANIFEST["skill_template"]["instruction"])
    upgraded["skill_template"]["description"] = "Newer shipped description."
    await _ship(app_session, tenant, upgraded)
    skill = await _skill(app_session, tenant)
    assert skill.description == "Our description."
    assert skill.shipped_update_at is not None


async def test_reset_brings_back_the_shipped_description_and_category(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    await _ship(app_session, tenant, SKILL_MANIFEST)
    shipped = await _skill(app_session, tenant)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        await _patch(c, tenant, shipped.id, description="Our description.", category="operations")
        r = await c.post(f"/api/v1/skills/{shipped.id}/reset", json={}, headers=_h(tenant))
        assert r.status_code == 200, r.text
        assert r.json()["description"] == "Checks invoices against the PO."
        assert r.json()["category"] == "finance"
        assert r.json()["customized"] is False
    skill = await _skill(app_session, tenant)
    assert skill.description == "Checks invoices against the PO."
    assert skill.category == "finance"


async def test_saving_an_unchanged_description_does_not_customise_a_shipped_skill(
    app_session: AppSessionFactory,
) -> None:
    """The drawer sends the description on every save; an unchanged one is not an edit."""
    tenant = uuid.uuid4()
    await _ship(app_session, tenant, SKILL_MANIFEST)
    shipped = await _skill(app_session, tenant)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        got = await _patch(c, tenant, shipped.id, description=shipped.description)
        assert got["customized"] is False
