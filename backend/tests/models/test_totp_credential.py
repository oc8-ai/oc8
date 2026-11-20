# backend/tests/models/test_totp_credential.py
from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_totp_credential_round_trips(app_session: AppSessionFactory) -> None:
    from oc8.constants import ACME_TENANT_ID

    async with app_session(ACME_TENANT_ID) as db:
        member = m.OrgMember(
            tenant_id=ACME_TENANT_ID,
            subject="totp-model-test@example.com",
            subject_uuid=uuid.uuid5(uuid.NAMESPACE_URL, "oc8:local:totp-model-test@example.com"),
        )
        db.add(member)
        await db.flush()

        cred = m.TotpCredential(
            tenant_id=ACME_TENANT_ID,
            member_id=member.id,
            secret_ref=f"totp:{member.id}",
            backup_codes=[{"hash": "argon2id$fake", "used_at": None}],
        )
        db.add(cred)
        await db.flush()

        assert cred.enrolled_at is None
        assert cred.backup_codes == [{"hash": "argon2id$fake", "used_at": None}]


async def test_org_member_and_organization_gain_totp_fields(
    app_session: AppSessionFactory,
) -> None:
    # A fresh tenant id, not ACME_TENANT_ID: this test inserts a singleton
    # Organization row, and ACME_TENANT_ID already has one committed by
    # tests/auth/test_password_auth.py's org_in_db fixture with no per-test
    # rollback (app_session commits) -- sharing it collides on a full-suite
    # run (organization_pkey), matching test_organization_onboarding_status.py's
    # own established pattern for this exact situation.
    tenant_id = uuid.uuid4()

    async with app_session(tenant_id) as db:
        org = m.Organization(
            id=tenant_id,
            slug=f"totp-test-{tenant_id.hex[:8]}",
            name="TOTP Test Org",
            settings={},
        )
        db.add(org)
        await db.flush()

        org = await db.get(m.Organization, tenant_id)
        assert org is not None

        member = m.OrgMember(
            tenant_id=tenant_id,
            subject="totp-grace-test@example.com",
            subject_uuid=uuid.uuid5(uuid.NAMESPACE_URL, "oc8:local:totp-grace-test@example.com"),
        )
        db.add(member)
        await db.flush()
        assert member.totp_grace_started_at is None
