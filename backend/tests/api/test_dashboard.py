from __future__ import annotations

import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.constants import ACME_TENANT_ID
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _token(
    subject: str = "dashboard-user", tenant: uuid.UUID | None = None, role: str = "member"
) -> str:
    return get_identity_provider().mint(
        tenant_id=tenant or uuid.UUID(str(ACME_TENANT_ID)), subject=subject, role=role
    )


async def test_get_layout_returns_null_when_the_caller_has_no_row() -> None:
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(subject='no-layout-yet')}"}
            r = await client.get("/api/v1/dashboard/layout", headers=headers)
            assert r.status_code == 200
            assert r.json() is None


async def test_put_layout_creates_then_get_returns_the_stored_shape() -> None:
    app = create_app()
    body = {
        "widgets": [
            {"id": "w1", "type": "chat", "x": 0, "y": 0, "w": 6, "h": 6, "config": {}},
        ],
        "templateId": "focus-chat",
    }
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(subject='put-create')}"}
            put_r = await client.put("/api/v1/dashboard/layout", json=body, headers=headers)
            assert put_r.status_code == 200, put_r.text
            assert put_r.json()["widgets"] == body["widgets"]
            assert put_r.json()["templateId"] == "focus-chat"

            get_r = await client.get("/api/v1/dashboard/layout", headers=headers)
            assert get_r.status_code == 200
            assert get_r.json()["widgets"] == body["widgets"]
            assert get_r.json()["templateId"] == "focus-chat"


async def test_layout_accepts_a_data_board_tile() -> None:
    """A dashboard tile stores only its board id (and saved filters); the
    board itself is read through `/boards`, never from the layout."""
    app = create_app()
    tile = {
        "id": "w1",
        "type": "data_board",
        "x": 0,
        "y": 0,
        "w": 6,
        "h": 10,
        "config": {"boardId": str(uuid.uuid4()), "filters": {"preset": "year_to_date"}},
    }
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(subject='board-tile')}"}
            put_r = await client.put(
                "/api/v1/dashboard/layout", json={"widgets": [tile]}, headers=headers
            )
            assert put_r.status_code == 200, put_r.text
            get_r = await client.get("/api/v1/dashboard/layout", headers=headers)
    assert get_r.json()["widgets"] == [tile]


async def test_put_layout_fully_replaces_not_merges(app_session: AppSessionFactory) -> None:
    app = create_app()
    first = {
        "widgets": [{"id": "w1", "type": "chat", "x": 0, "y": 0, "w": 6, "h": 6, "config": {}}],
        "templateId": None,
    }
    second = {
        "widgets": [
            {"id": "w2", "type": "budget", "x": 0, "y": 0, "w": 3, "h": 3, "config": {}},
        ],
        "templateId": None,
    }
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(subject='put-replace')}"}
            await client.put("/api/v1/dashboard/layout", json=first, headers=headers)
            r = await client.put("/api/v1/dashboard/layout", json=second, headers=headers)
            assert r.status_code == 200
            assert r.json()["widgets"] == second["widgets"]

    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        member = (
            await db.execute(select(m.OrgMember).where(m.OrgMember.subject == "put-replace"))
        ).scalar_one()
        row = (
            await db.execute(
                select(m.MemberDashboardLayout).where(
                    m.MemberDashboardLayout.member_id == member.id
                )
            )
        ).scalar_one()
        assert row.widgets == second["widgets"]


async def test_dashboard_layout_is_isolated_per_member() -> None:
    app = create_app()
    body_a = {
        "widgets": [{"id": "wa", "type": "chat", "x": 0, "y": 0, "w": 6, "h": 6, "config": {}}],
        "templateId": None,
    }
    body_b = {
        "widgets": [
            {"id": "wb", "type": "reports", "x": 0, "y": 0, "w": 4, "h": 4, "config": {}},
        ],
        "templateId": None,
    }
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers_a = {"Authorization": f"Bearer {_token(subject='member-a')}"}
            headers_b = {"Authorization": f"Bearer {_token(subject='member-b')}"}
            await client.put("/api/v1/dashboard/layout", json=body_a, headers=headers_a)
            await client.put("/api/v1/dashboard/layout", json=body_b, headers=headers_b)

            get_a = await client.get("/api/v1/dashboard/layout", headers=headers_a)
            get_b = await client.get("/api/v1/dashboard/layout", headers=headers_b)
            assert get_a.json()["widgets"] == body_a["widgets"]
            assert get_b.json()["widgets"] == body_b["widgets"]


async def test_get_layout_returns_a_retired_widget_type_instead_of_500(
    app_session: AppSessionFactory,
) -> None:
    """A widget type that is renamed or retired after a member already has an
    instance of it stored must not turn `GET /dashboard/layout` into a
    permanent 500 -- they would be unable to even load their dashboard to
    remove the offending tile. Insert the unrecognized type directly
    (bypassing the write path's `Literal` validation, which correctly still
    rejects it on `PUT`) to simulate exactly that."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        member = m.OrgMember(
            tenant_id=tenant, subject="retired-widget-owner", subject_uuid=uuid.uuid4()
        )
        db.add(member)
        await db.flush()
        row = m.MemberDashboardLayout(
            tenant_id=tenant,
            member_id=member.id,
            widgets=[
                {
                    "id": "w1",
                    "type": "some-retired-type",
                    "x": 0,
                    "y": 0,
                    "w": 4,
                    "h": 4,
                    "config": {},
                }
            ],
            template_id=None,
        )
        db.add(row)

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(subject='retired-widget-owner')}"}
            r = await client.get("/api/v1/dashboard/layout", headers=headers)
            assert r.status_code == 200, r.text
            assert r.json()["widgets"][0]["type"] == "some-retired-type"


async def test_get_templates_returns_exactly_four_with_widgets() -> None:
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(subject='templates-reader')}"}
            r = await client.get("/api/v1/dashboard/templates", headers=headers)
            assert r.status_code == 200
            templates = r.json()
            assert len(templates) == 4
            for template in templates:
                assert template["widgets"], template["id"]
                assert set(template["name"]) == {"en", "de"}


async def test_command_center_template_leads_with_a_dominant_chat_tile() -> None:
    """The whole point of this template: chat is the largest tile, not one
    among equals -- see the "Copilot as primary interface" comment on
    `_TEMPLATES` in `dashboard.py`."""
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(subject='templates-reader')}"}
            r = await client.get("/api/v1/dashboard/templates", headers=headers)
            assert r.status_code == 200
            templates = {t["id"]: t for t in r.json()}
            command_center = templates["command-center"]
            widgets_by_type = {w["type"]: w for w in command_center["widgets"]}
            assert "tasks" in widgets_by_type
            chat_area = widgets_by_type["chat"]["w"] * widgets_by_type["chat"]["h"]
            for wtype, w in widgets_by_type.items():
                if wtype == "chat":
                    continue
                assert chat_area > w["w"] * w["h"], f"chat should dominate over {wtype}"


async def test_templates_use_forty_pixel_rows() -> None:
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(subject='templates-reader')}"}
            r = await client.get("/api/v1/dashboard/templates", headers=headers)
            assert r.status_code == 200
            by_id = {t["id"]: {w["type"]: w for w in t["widgets"]} for t in r.json()}

    def box(w: dict[str, Any]) -> dict[str, int]:
        return {k: w[k] for k in ("x", "y", "w", "h")}

    assert box(by_id["command-center"]["chat"]) == {"x": 0, "y": 0, "w": 8, "h": 16}
    assert box(by_id["overview"]["budget"]) == {"x": 0, "y": 10, "w": 6, "h": 6}


@pytest.mark.parametrize(
    "patch",
    [{"y": 10001}, {"h": 1001}, {"h": 0}, {"x": 13}, {"x": -1}, {"w": 0}, {"w": 13}, {"y": -1}],
)
async def test_out_of_range_widget_positions_are_refused(patch: dict[str, int]) -> None:
    widget = {"id": "w1", "type": "chat", "x": 0, "y": 0, "w": 6, "h": 6, "config": {}} | patch
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(subject='bounds-writer')}"}
            put_r = await client.put(
                "/api/v1/dashboard/layout", json={"widgets": [widget]}, headers=headers
            )
            post_r = await client.post(
                "/api/v1/dashboard/presets",
                json={"name": "x", "scope": "personal", "widgets": [widget]},
                headers=headers,
            )
    assert put_r.status_code == 422, put_r.text
    assert post_r.status_code == 422, post_r.text


def _preset_body(name: str = "My layout", scope: str = "personal") -> dict[str, Any]:
    return {
        "name": name,
        "scope": scope,
        "widgets": [{"id": "w1", "type": "chat", "x": 0, "y": 0, "w": 6, "h": 6, "config": {}}],
    }


async def test_post_preset_creates_a_personal_preset_visible_only_to_its_owner() -> None:
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            owner = {"Authorization": f"Bearer {_token(subject='preset-owner')}"}
            other = {"Authorization": f"Bearer {_token(subject='preset-other')}"}

            create = await client.post(
                "/api/v1/dashboard/presets", json=_preset_body(), headers=owner
            )
            assert create.status_code == 201, create.text
            body = create.json()
            assert body["scope"] == "personal"
            assert body["mine"] is True

            mine = await client.get("/api/v1/dashboard/presets", headers=owner)
            assert [p["id"] for p in mine.json()] == [body["id"]]

            theirs = await client.get("/api/v1/dashboard/presets", headers=other)
            assert theirs.json() == []


async def test_post_preset_rejects_tenant_scope_without_settings_manage() -> None:
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            member = {"Authorization": f"Bearer {_token(subject='preset-member')}"}
            r = await client.post(
                "/api/v1/dashboard/presets", json=_preset_body(scope="tenant"), headers=member
            )
            assert r.status_code == 403


async def test_post_preset_with_tenant_scope_by_an_admin_is_visible_to_other_members() -> None:
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            admin = {"Authorization": f"Bearer {_token(subject='preset-admin', role='org_admin')}"}
            member = {"Authorization": f"Bearer {_token(subject='preset-member2')}"}

            create = await client.post(
                "/api/v1/dashboard/presets",
                json=_preset_body(name="Team layout", scope="tenant"),
                headers=admin,
            )
            assert create.status_code == 201, create.text
            preset_id = create.json()["id"]

            seen = await client.get("/api/v1/dashboard/presets", headers=member)
            assert [p["id"] for p in seen.json()] == [preset_id]
            assert seen.json()[0]["mine"] is False


async def test_delete_preset_by_a_non_owner_without_settings_manage_is_refused() -> None:
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            owner = {"Authorization": f"Bearer {_token(subject='preset-del-owner')}"}
            other = {"Authorization": f"Bearer {_token(subject='preset-del-other')}"}

            create = await client.post(
                "/api/v1/dashboard/presets", json=_preset_body(), headers=owner
            )
            preset_id = create.json()["id"]

            r = await client.delete(f"/api/v1/dashboard/presets/{preset_id}", headers=other)
            assert r.status_code == 403

            r = await client.delete(f"/api/v1/dashboard/presets/{preset_id}", headers=owner)
            assert r.status_code == 204

            # Only asserts the deleted preset is gone, not that the list is
            # empty: this tenant's row is shared with the other tests in this
            # module, some of which leave a `scope="tenant"` preset behind.
            seen = await client.get("/api/v1/dashboard/presets", headers=owner)
            assert preset_id not in [p["id"] for p in seen.json()]


async def test_delete_tenant_preset_by_an_admin_who_did_not_create_it_succeeds() -> None:
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            creator = {
                "Authorization": f"Bearer {_token(subject='preset-creator', role='org_admin')}"
            }
            other_admin = {
                "Authorization": f"Bearer {_token(subject='preset-other-admin', role='org_admin')}"
            }

            create = await client.post(
                "/api/v1/dashboard/presets",
                json=_preset_body(scope="tenant"),
                headers=creator,
            )
            preset_id = create.json()["id"]

            r = await client.delete(f"/api/v1/dashboard/presets/{preset_id}", headers=other_admin)
            assert r.status_code == 204
