"""`ck_department_personal_frame_closed`: a personal department's frame is closed.

The PDP ignores an agent-exclusive grant on a closed frame, so a personal
department stored without the marker would silently re-open that door. The
database refuses it.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from oc8 import models as m
from oc8.authz.scope import subject_uuid_for
from oc8.models.core import DEPARTMENT_KIND_PERSONAL
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _try(app_session: AppSessionFactory, frame: dict[str, object]) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        member = m.OrgMember(
            tenant_id=tenant,
            subject="ada",
            subject_uuid=subject_uuid_for("ada"),
            display_name="Ada",
        )
        db.add(member)
        await db.flush()
        db.add(
            m.Department(
                tenant_id=tenant,
                name="Ada",
                kind=DEPARTMENT_KIND_PERSONAL,
                owner_member_id=member.id,
                frame=frame,
            )
        )
        await db.flush()


async def test_a_personal_department_without_the_marker_is_refused(
    app_session: AppSessionFactory,
) -> None:
    with pytest.raises(IntegrityError, match="ck_department_personal_frame_closed"):
        await _try(app_session, {"tools": {}})
    with pytest.raises(IntegrityError, match="ck_department_personal_frame_closed"):
        await _try(app_session, {"closed": False, "tools": {}})


async def test_a_closed_personal_frame_is_accepted(app_session: AppSessionFactory) -> None:
    await _try(app_session, {"closed": True, "tools": {}})
