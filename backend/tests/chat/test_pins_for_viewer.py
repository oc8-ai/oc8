from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.chat.context.base import viewer_for_member
from oc8.chat.projects import (
    PinRefused,
    authorise_pins,
    delete_member_projects,
    pinned_refs_for_viewer,
)
from oc8.copilot.profile import offboard_member
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_pins_are_rechecked_for_each_viewer(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = m.KnowledgeBase(tenant_id=tenant, name="Board minutes", embedding_model="x")
        admin = m.OrgMember(tenant_id=tenant, subject="admin", subject_uuid=uuid.uuid4())
        emp = m.OrgMember(tenant_id=tenant, subject="emp", subject_uuid=uuid.uuid4())
        db.add_all([kb, admin, emp])
        await db.flush()
        refs = [{"kind": "knowledge_base", "id": str(kb.id), "label": "Board minutes"}]
        as_admin = await pinned_refs_for_viewer(
            db, viewer=await viewer_for_member(db, admin, token_role="org_admin"), refs=refs
        )
        as_emp = await pinned_refs_for_viewer(
            db, viewer=await viewer_for_member(db, emp, token_role="member"), refs=refs
        )
    assert as_admin[0].available is True
    assert as_emp[0].available is False and as_emp[0].label == "Board minutes"


async def test_authorised_pins_store_the_canonical_id_and_the_rows_label(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = m.KnowledgeBase(tenant_id=tenant, name="Prices", embedding_model="x")
        admin = m.OrgMember(tenant_id=tenant, subject="admin", subject_uuid=uuid.uuid4())
        db.add_all([kb, admin])
        await db.flush()
        viewer = await viewer_for_member(db, admin, token_role="org_admin")
        spelled = str(kb.id).upper()
        stored = await authorise_pins(
            db,
            viewer=viewer,
            refs=[
                {"kind": "knowledge_base", "id": spelled},
                {"kind": "knowledge_base", "id": str(kb.id)},
            ],
        )
    # Kept once, under its canonical id, labelled from the row.
    assert stored == [{"kind": "knowledge_base", "id": str(kb.id), "label": "Prices"}]


async def test_refused_pins_name_only_what_the_client_sent(app_session: AppSessionFactory) -> None:
    tenant, other = uuid.uuid4(), uuid.uuid4()
    async with app_session(other) as db:
        theirs = m.KnowledgeBase(tenant_id=other, name="Their secret", embedding_model="x")
        db.add(theirs)
        await db.flush()
        theirs_id = theirs.id
    async with app_session(tenant) as db:
        admin = m.OrgMember(tenant_id=tenant, subject="admin", subject_uuid=uuid.uuid4())
        db.add(admin)
        await db.flush()
        viewer = await viewer_for_member(db, admin, token_role="org_admin")
        missing = str(uuid.uuid4())
        with pytest.raises(PinRefused) as caught:
            await authorise_pins(
                db,
                viewer=viewer,
                refs=[
                    {"kind": "knowledge_base", "id": str(theirs_id)},
                    {"kind": "knowledge_base", "id": missing},
                    {"kind": "nonsense", "id": "x"},
                ],
            )
    # Another tenant's object and one that never existed read the same.
    assert caught.value.refused == [
        {"kind": "knowledge_base", "id": str(theirs_id)},
        {"kind": "knowledge_base", "id": missing},
        {"kind": "nonsense", "id": "x"},
    ]


async def test_offboarding_removes_the_members_projects(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        gone = m.OrgMember(tenant_id=tenant, subject="gone", subject_uuid=uuid.uuid4())
        stays = m.OrgMember(tenant_id=tenant, subject="stays", subject_uuid=uuid.uuid4())
        db.add_all([gone, stays])
        await db.flush()
        theirs = m.ChatProject(tenant_id=tenant, owner_member_id=gone.id, name="Mine")
        kept = m.ChatProject(tenant_id=tenant, owner_member_id=stays.id, name="Kept")
        db.add_all([theirs, kept])
        await db.flush()
        session = m.ChatSession(
            tenant_id=tenant, agent_id=uuid.uuid4(), member_id=gone.id, project_id=theirs.id
        )
        db.add(session)
        await db.flush()
        await offboard_member(db, tenant_id=tenant, member_id=gone.id)
        names = set((await db.execute(select(m.ChatProject.name))).scalars())
        await db.refresh(session)
        assert names == {"Kept"}
        assert session.project_id is None
        # Idempotent: a second sweep finds nothing to do.
        await delete_member_projects(db, tenant_id=tenant, member_id=gone.id)
