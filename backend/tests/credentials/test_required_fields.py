"""A login is complete or refused; enablement for departments; references
that keep a login from being deleted silently."""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator

import pytest

from oc8 import models as m
from oc8.capas.manifest import CredentialTypeSpec, SetupFieldSpec
from oc8.credentials import registry
from oc8.credentials.enablement import enabled_credentials, set_departments
from oc8.credentials.service import (
    CredentialInUse,
    IncompleteCredential,
    create_credential,
    delete_credential,
    missing_required,
    update_credential,
)
from oc8.secrets.service import resolve_secret
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_TYPE = "test_required_login"


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    from oc8 import config

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )


@pytest.fixture(autouse=True)
def _login_type() -> Iterator[None]:
    registry.CORE_CREDENTIAL_TYPES[_TYPE] = CredentialTypeSpec(
        name=_TYPE,
        display_name="Test login",
        fields=[
            SetupFieldSpec(key="url", label="URL", kind="url"),
            SetupFieldSpec(key="username", label="User", kind="text"),
            SetupFieldSpec(key="password", label="Password", kind="password"),
            SetupFieldSpec(key="note", label="Note", kind="text", required=False),
        ],
    )
    try:
        yield
    finally:
        registry.CORE_CREDENTIAL_TYPES.pop(_TYPE, None)


_FULL = {"url": "https://tool.acme.test", "username": "anna", "password": "s3cret"}


async def _login(
    db: object, tenant: uuid.UUID, name: str = "Tool", owner: uuid.UUID | None = None
) -> m.Credential:
    return await create_credential(
        db,  # type: ignore[arg-type]
        tenant_id=tenant,
        name=name,
        credential_type=_TYPE,
        field_values=dict(_FULL),
        owner_member_id=owner,
    )


async def _department(db: object, tenant: uuid.UUID, name: str) -> uuid.UUID:
    dept = m.Department(tenant_id=tenant, name=name)
    db.add(dept)  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    return dept.id


async def _member(db: object, tenant: uuid.UUID) -> uuid.UUID:
    row = m.OrgMember(
        tenant_id=tenant,
        subject="anna@acme.test",
        subject_uuid=uuid.uuid4(),
        display_name="Anna",
        email="anna@acme.test",
    )
    db.add(row)  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    return row.id


async def _agent(db: object, tenant: uuid.UUID) -> uuid.UUID:
    dept = await _department(db, tenant, "Sales")
    agent = m.Agent(tenant_id=tenant, department_id=dept, name="Ada")
    db.add(agent)  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    return agent.id


def test_missing_required_counts_a_stored_secret_as_present() -> None:
    spec = registry.CORE_CREDENTIAL_TYPES[_TYPE]
    assert missing_required(spec, {"url": " ", "username": "anna"}, {}) == ["url", "password"]
    assert (
        missing_required(
            spec, {"url": "https://x.test", "username": "anna"}, {"password": "credential/x/p"}
        )
        == []
    )


async def test_a_login_without_its_required_url_is_refused(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        with pytest.raises(IncompleteCredential) as exc:
            await create_credential(
                db,
                tenant_id=tenant,
                name="Tool",
                credential_type=_TYPE,
                field_values={"url": "  ", "username": "anna", "password": "s3cret"},
            )
    assert exc.value.missing == ["url"]


async def test_clearing_a_required_field_on_update_is_refused(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cred = await _login(db, tenant)
        with pytest.raises(IncompleteCredential) as exc:
            await update_credential(
                db, tenant_id=tenant, credential_id=cred.id, field_values={"username": ""}
            )
        assert exc.value.missing == ["username"]
        # A blank secret means "unchanged": not a clearing.
        await update_credential(
            db, tenant_id=tenant, credential_id=cred.id, field_values={"password": ""}
        )
        assert cred.field_values["username"] == "anna"


async def test_a_personal_login_cannot_be_enabled_for_a_department(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cred = await _login(db, tenant, owner=await _member(db, tenant))
        dept = await _department(db, tenant, "Sales")
        with pytest.raises(ValueError, match="personal"):
            await set_departments(db, cred, department_ids={dept}, all_departments=False)
        with pytest.raises(ValueError, match="personal"):
            await set_departments(db, cred, department_ids=set(), all_departments=True)


async def test_enabled_credentials_include_all_departments_logins(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        sales = await _department(db, tenant, "Sales")
        support = await _department(db, tenant, "Support")
        direct = await _login(db, tenant, "Direct")
        everyone = await _login(db, tenant, "Everyone")
        elsewhere = await _login(db, tenant, "Elsewhere")
        await _login(db, tenant, "Mine", owner=await _member(db, tenant))
        await set_departments(db, direct, department_ids={sales}, all_departments=False)
        await set_departments(db, everyone, department_ids=set(), all_departments=True)
        await set_departments(db, elsewhere, department_ids={support}, all_departments=False)
        found = await enabled_credentials(db, credential_type=_TYPE, department_id=sales)
        assert [c.name for c in found] == ["Direct", "Everyone"]
        # Replacing, not adding.
        await set_departments(db, direct, department_ids={support}, all_departments=False)
        found = await enabled_credentials(db, credential_type=_TYPE, department_id=sales)
        assert [c.name for c in found] == ["Everyone"]
        assert direct.all_departments is False


async def test_a_login_used_by_an_agent_is_not_deleted_silently(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id = await _agent(db, tenant)
        cred = await _login(db, tenant)
        access = m.AgentToolAccess(
            tenant_id=tenant,
            agent_id=agent_id,
            tool_key="tool",
            mode="fixed",
            credential_id=cred.id,
        )
        db.add(access)
        await db.flush()
        with pytest.raises(CredentialInUse) as exc:
            await delete_credential(db, tenant_id=tenant, credential_id=cred.id)
        assert exc.value.references
        await delete_credential(db, tenant_id=tenant, credential_id=cred.id, force=True)
        assert await db.get(m.Credential, cred.id) is None
        await db.refresh(access)
        # The agent's choice now names nothing: the runtime refuses rather
        # than picking another login.
        assert access.mode == "fixed" and access.credential_id is None


async def test_deleting_one_of_two_credentials_sharing_a_secret_ref_keeps_the_secret(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        first = await _login(db, tenant, "First")
        ref = first.secret_refs["password"]
        second = m.Credential(
            tenant_id=tenant,
            name="Second",
            credential_type=_TYPE,
            field_values=dict(first.field_values),
            secret_refs={"password": ref},
        )
        db.add(second)
        await db.flush()
        await delete_credential(db, tenant_id=tenant, credential_id=first.id)
        assert await resolve_secret(db, tenant_id=tenant, ref=ref) == "s3cret"
        # A credential deletes only secrets it owns (`credential/{its id}/...`):
        # the first one's secret is not the second's to remove.
        await delete_credential(db, tenant_id=tenant, credential_id=second.id)
        assert await resolve_secret(db, tenant_id=tenant, ref=ref) == "s3cret"
