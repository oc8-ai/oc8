"""A customised shipped skill survives a backup and restore: its shipped
version, its customised flag and a waiting shipped update all come back."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.backup.archive import export_archive
from oc8.backup.service import restore_from_archive
from tests.backup.test_restore_service import (  # noqa: F401
    _kek,
    acme_tenant,
    actor,
    other_instance,
    other_tenant,
)
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _definition(text: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "slug": "invoice-check",
        "version": "1.0.0",
        "instruction": text,
        "requires": {"tools": [], "kbs": []},
        "guardrails": [],
    }


async def test_shipped_skill_tracking_survives_a_restore_into_another_instance(
    app_session: AppSessionFactory,
    other_instance: AppSessionFactory,  # noqa: F811
    acme_tenant: uuid.UUID,  # noqa: F811
    other_tenant: uuid.UUID,  # noqa: F811
    actor: uuid.UUID,  # noqa: F811
) -> None:
    customized = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.UTC)
    waiting = dt.datetime(2026, 11, 2, 12, 0, tzinfo=dt.UTC)
    async with app_session(acme_tenant) as db:
        skill = m.Skill(
            tenant_id=acme_tenant,
            name="Invoice check",
            description="",
            author="acme.invoice-skill",
            origin="store",
            trust_level="first_party",
            customized_at=customized,
            shipped_update_at=waiting,
        )
        db.add(skill)
        await db.flush()
        shipped = m.SkillVersion(
            tenant_id=acme_tenant,
            skill_id=skill.id,
            semver="1.1.0",
            definition=_definition("Shipped."),
            artifact_hash=b"s",
        )
        edited = m.SkillVersion(
            tenant_id=acme_tenant,
            skill_id=skill.id,
            semver="1.0.1",
            definition=_definition("Ours."),
            artifact_hash=b"e",
        )
        db.add_all([shipped, edited])
        await db.flush()
        skill.shipped_version_id = shipped.id
        skill.current_version_id = edited.id
        await db.flush()
        skill_id, shipped_id, edited_id = skill.id, shipped.id, edited.id
        _, backup = await export_archive(db, tenant_id=acme_tenant, passphrase=None)

    async with other_instance(other_tenant) as target:
        await restore_from_archive(
            target,
            tenant_id=other_tenant,
            archive_bytes=backup,
            passphrase=None,
            actor_type="human",
            actor_id=actor,
        )
        restored = (
            await target.execute(select(m.Skill).where(m.Skill.id == skill_id))
        ).scalar_one()
        assert restored.tenant_id == other_tenant
        assert restored.shipped_version_id == shipped_id
        assert restored.current_version_id == edited_id
        assert restored.customized_at == customized
        assert restored.shipped_update_at == waiting
