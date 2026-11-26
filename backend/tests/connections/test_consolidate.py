"""Turning per-department tool setups into shared logins (unified connections
design, "Migration"): `connections.consolidate.consolidate_tenant`.

Built on a throwaway tool pack and login type, so no vendor is involved."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from oc8 import config
from oc8 import models as m
from oc8.agents.versioning import META_KEY, payload_hash
from oc8.capas.discovery import invalidate_discovery_cache
from oc8.capas.manifest import CredentialTypeSpec, SetupFieldSpec
from oc8.connections.access import (
    binding_fingerprint,
    resolve_tool_access,
    trigger_fingerprint,
    unconfirmed_reason,
)
from oc8.connections.confirmation import stamp_binding, stamp_trigger, trigger_confirmed
from oc8.connections.consolidate import consolidate_tenant
from oc8.credentials import registry
from oc8.credentials.enablement import department_ids_by_credential, set_departments
from oc8.credentials.service import delete_credential, missing_required, update_credential
from oc8.runtime.executor import _resolve_mcp_connection
from oc8.secrets.service import SecretNotFound, resolve_secret, store_secret
from tests.conftest import AppSessionFactory
from tests.connections.helpers import api, enable_plugin_row, headers, member
from tests.factories import _make_agent

pytestmark = pytest.mark.asyncio

PLUGIN = "test_fold_pack"
KEY = "test_fold_tool"
TYPE = "test_fold_login"
TOKEN_REF = f"plugin:{PLUGIN}:token"

PLUGIN_TOML = f"""
[plugin]
name = "{PLUGIN}"
label = "Fold Tool"
version = "1.0.0"
type = "tool_pack"
trust = "first_party"
summary = "A throwaway tool pack whose bridge takes a login"
permissions = ["mcp:connect", "secrets:use"]
"""

TOOL_PACK_TOML = f"""
[[connections]]
key = "primary"
name = "{KEY}"
transport = "stdio"
server_url = ""
credential_type = "{TYPE}"

[connections.scopes]
read = ["look"]
modify = ["change"]

[connections.credential_env_fields]
FOLD_URL = "url"
FOLD_TOKEN = "token"

[connections.config]
command = "python"
args = ["-m", "bridge"]

[connections.config.env]
FOLD_MODE = "plain"
"""

FRAME = {"tools": {KEY: {"enabled": True, "read": True, "modify": True}}}
NARROWING = {"tools": {KEY: {"enabled": True, "read": True, "modify": True}}}


@pytest.fixture(autouse=True)
def _test_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _connection_settings: None
) -> Iterator[None]:
    pack = tmp_path / "capas" / PLUGIN
    pack.mkdir(parents=True)
    (pack / "plugin.toml").write_text(PLUGIN_TOML)
    (pack / "tool_pack.toml").write_text(TOOL_PACK_TOML)
    real = config.get_settings().capas_path
    monkeypatch.setenv("OC8_CAPAS_PATH", f"{tmp_path / 'capas'},{real}")
    kek = config.get_settings().secret_kek
    config.get_settings.cache_clear()
    monkeypatch.setattr(config.get_settings(), "secret_kek", kek, raising=False)
    invalidate_discovery_cache()
    registry.CORE_CREDENTIAL_TYPES[TYPE] = CredentialTypeSpec(
        name=TYPE,
        display_name="Fold login",
        fields=[
            SetupFieldSpec(key="url", label="URL", kind="url"),
            SetupFieldSpec(key="token", label="Token", kind="password", required=False),
        ],
    )
    try:
        yield
    finally:
        registry.CORE_CREDENTIAL_TYPES.pop(TYPE, None)
        invalidate_discovery_cache()


# ------------------------------------------------------------------ fixtures


def _config(url: str | None = None, token_ref: str | None = None) -> dict[str, Any]:
    env: dict[str, str] = {"FOLD_MODE": "plain"}
    if url is not None:
        env["FOLD_URL"] = url
    return {
        "_plugin_name": PLUGIN,
        "_connection_key": "primary",
        "command": "python",
        "args": ["-m", "bridge"],
        "env": env,
        "secret_env": {"FOLD_TOKEN": token_ref} if token_ref else {},
    }


async def _tenant(app_session: AppSessionFactory) -> m.Agent:
    """An agent in department "Eng" (frame enabling the tool), the plugin
    enabled, and the shared token stored."""
    agent = await _make_agent(app_session)
    async with app_session(agent.tenant_id) as db:
        dept = await db.get(m.Department, agent.department_id)
        assert dept is not None
        dept.frame = FRAME
        await store_secret(
            db, tenant_id=agent.tenant_id, name=TOKEN_REF, value="t0ken", kind="plugin"
        )
    await enable_plugin_row(app_session, agent.tenant_id, PLUGIN)
    return agent


async def _department(app_session: AppSessionFactory, tenant: uuid.UUID, name: str) -> uuid.UUID:
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name=name, frame=FRAME)
        db.add(dept)
        await db.flush()
        return dept.id


async def _row(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    *,
    department_id: uuid.UUID | None,
    url: str | None = "https://svc.acme.test",
    token_ref: str | None = TOKEN_REF,
    credential_id: uuid.UUID | None = None,
    age: int = 0,
) -> uuid.UUID:
    """A tool row; `age` minutes in the past (older rows first)."""
    async with app_session(tenant) as db:
        row = m.McpConnection(
            tenant_id=tenant,
            name=KEY,
            server_url="",
            transport="stdio",
            connected=True,
            config=_config(url, token_ref),
            department_id=department_id,
            credential_id=credential_id,
            created_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=age),
        )
        db.add(row)
        await db.flush()
        return row.id


async def _consolidate(
    app_session: AppSessionFactory, tenant: uuid.UUID, *, dry_run: bool = False
) -> Any:
    async with app_session(tenant) as db:
        return await consolidate_tenant(db, tenant, dry_run=dry_run)


async def _creds(app_session: AppSessionFactory, tenant: uuid.UUID) -> list[m.Credential]:
    async with app_session(tenant) as db:
        return list(
            (
                await db.execute(
                    select(m.Credential)
                    .where(m.Credential.credential_type == TYPE)
                    .order_by(m.Credential.created_at)
                )
            ).scalars()
        )


async def _rows(app_session: AppSessionFactory, tenant: uuid.UUID) -> list[m.McpConnection]:
    async with app_session(tenant) as db:
        return list(
            (await db.execute(select(m.McpConnection).where(m.McpConnection.name == KEY))).scalars()
        )


async def _enabled(
    app_session: AppSessionFactory, tenant: uuid.UUID, cred_id: uuid.UUID
) -> set[uuid.UUID]:
    async with app_session(tenant) as db:
        return set((await department_ids_by_credential(db, [cred_id]))[cred_id])


# ------------------------------------------------------------------ step 1


async def test_two_departments_with_the_same_login_share_one_credential(
    app_session: AppSessionFactory,
) -> None:
    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    sales = await _department(app_session, tenant, "Sales")
    first = await _row(app_session, tenant, department_id=agent.department_id, age=10)
    second = await _row(app_session, tenant, department_id=sales, age=5)

    report = await _consolidate(app_session, tenant)

    creds = await _creds(app_session, tenant)
    assert len(creds) == 1
    cred = creds[0]
    assert cred.name == "Fold Tool — Eng"
    assert cred.owner_member_id is None
    assert cred.field_values == {"url": "https://svc.acme.test"}
    assert cred.secret_refs == {"token": f"credential/{cred.id}/token"}
    assert sorted(cred.consolidated_from or []) == sorted([str(first), str(second)])
    assert await _enabled(app_session, tenant, cred.id) == {agent.department_id, sales}
    rows = await _rows(app_session, tenant)
    assert [r.id for r in rows] == [first]
    tool_row = rows[0]
    assert tool_row.department_id is None and tool_row.credential_id is None
    assert tool_row.config["env"] == {"FOLD_MODE": "plain"}
    assert tool_row.config["secret_env"] == {}
    assert (report.created, report.enabled, report.collapsed) == (1, 2, 1)


async def test_different_logins_become_two_credentials_each_enabled_once(
    app_session: AppSessionFactory,
) -> None:
    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    sales = await _department(app_session, tenant, "Sales")
    await _row(app_session, tenant, department_id=agent.department_id, url="https://a.test", age=9)
    await _row(app_session, tenant, department_id=sales, url="https://b.test", age=3)

    report = await _consolidate(app_session, tenant)

    creds = await _creds(app_session, tenant)
    by_url = {c.field_values["url"]: c for c in creds}
    assert set(by_url) == {"https://a.test", "https://b.test"}
    assert by_url["https://a.test"].name == "Fold Tool — Eng"
    assert by_url["https://b.test"].name == "Fold Tool — Sales"
    assert await _enabled(app_session, tenant, by_url["https://a.test"].id) == {agent.department_id}
    assert await _enabled(app_session, tenant, by_url["https://b.test"].id) == {sales}
    assert (report.created, report.enabled, report.collapsed) == (2, 2, 1)


async def test_an_existing_equal_login_is_reused(app_session: AppSessionFactory) -> None:
    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    async with app_session(tenant) as db:
        existing = m.Credential(
            tenant_id=tenant,
            name="Our login",
            credential_type=TYPE,
            field_values={"url": "https://svc.acme.test"},
            secret_refs={"token": TOKEN_REF},
        )
        db.add(existing)
        await db.flush()
        existing_id = existing.id
    await _row(app_session, tenant, department_id=agent.department_id)

    report = await _consolidate(app_session, tenant)

    assert [c.id for c in await _creds(app_session, tenant)] == [existing_id]
    assert await _enabled(app_session, tenant, existing_id) == {agent.department_id}
    assert report.created == 0


async def test_an_empty_url_survives_as_an_incomplete_login(
    app_session: AppSessionFactory,
) -> None:
    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    await _row(app_session, tenant, department_id=agent.department_id, url="")

    await _consolidate(app_session, tenant)

    creds = await _creds(app_session, tenant)
    assert len(creds) == 1
    cred = creds[0]
    assert cred.secret_refs == {"token": f"credential/{cred.id}/token"}
    assert missing_required(
        registry.CORE_CREDENTIAL_TYPES[TYPE], cred.field_values, cred.secret_refs
    ) == ["url"]
    assert await _enabled(app_session, tenant, cred.id) == {agent.department_id}


async def test_consolidated_login_owns_a_copy_of_its_secret(
    app_session: AppSessionFactory,
) -> None:
    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    sales = await _department(app_session, tenant, "Sales")
    # Every department's old setup overwrote the same secret name.
    await _row(app_session, tenant, department_id=agent.department_id, url="https://a.test")
    await _row(app_session, tenant, department_id=sales, url="https://b.test")
    await _consolidate(app_session, tenant)
    first, second = await _creds(app_session, tenant)
    assert first.secret_refs == {"token": f"credential/{first.id}/token"}
    assert second.secret_refs == {"token": f"credential/{second.id}/token"}

    async with app_session(tenant) as db:
        for cred in (first, second):
            ref = cred.secret_refs["token"]
            assert await resolve_secret(db, tenant_id=tenant, ref=ref) == "t0ken"
        await delete_credential(db, tenant_id=tenant, credential_id=first.id, force=True)
    async with app_session(tenant) as db:
        # The old setup's secret is untouched, the other login keeps its copy.
        assert await resolve_secret(db, tenant_id=tenant, ref=TOKEN_REF) == "t0ken"
        ref = second.secret_refs["token"]
        assert await resolve_secret(db, tenant_id=tenant, ref=ref) == "t0ken"
        with pytest.raises(SecretNotFound):
            await resolve_secret(db, tenant_id=tenant, ref=first.secret_refs["token"])


async def test_deleting_a_login_never_deletes_a_secret_it_does_not_own(
    app_session: AppSessionFactory,
) -> None:
    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    async with app_session(tenant) as db:
        cred = m.Credential(
            tenant_id=tenant,
            name="Points elsewhere",
            credential_type=TYPE,
            field_values={"url": "https://svc.acme.test"},
            secret_refs={"token": TOKEN_REF},
        )
        db.add(cred)
        await db.flush()
        cred_id = cred.id
    async with app_session(tenant) as db:
        # A new value goes under the login's own name ...
        updated = await update_credential(
            db, tenant_id=tenant, credential_id=cred_id, field_values={"token": "new"}
        )
        assert updated.secret_refs == {"token": f"credential/{cred_id}/token"}
        assert await resolve_secret(db, tenant_id=tenant, ref=TOKEN_REF) == "t0ken"
        # ... and deleting never removes the one it did not own.
        updated.secret_refs = {"token": TOKEN_REF, "other": f"credential/{cred_id}/token"}
        await db.flush()
        await delete_credential(db, tenant_id=tenant, credential_id=cred_id, force=True)
    async with app_session(tenant) as db:
        assert await resolve_secret(db, tenant_id=tenant, ref=TOKEN_REF) == "t0ken"
        with pytest.raises(SecretNotFound):
            await resolve_secret(db, tenant_id=tenant, ref=f"credential/{cred_id}/token")


# ------------------------------------------------------------------ steps 2-3


async def test_a_held_frame_default_follows_the_collapsed_row(
    app_session: AppSessionFactory,
) -> None:
    """A loosening held for a department's owner (`pending_frame_tools`) names
    the successor too: a later confirmation must not restore a deleted row."""
    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    anna = await member(app_session, tenant, "anna@acme.test", name="Anna")
    keep = await _row(app_session, tenant, department_id=agent.department_id, age=10)
    async with app_session(tenant) as db:
        row = m.Department(
            tenant_id=tenant,
            name="Anna",
            kind="personal",
            owner_member_id=anna,
            frame={"closed": True, **FRAME},
        )
        db.add(row)
        await db.flush()
        personal = row.id
    gone = await _row(app_session, tenant, department_id=personal, age=5)
    async with app_session(tenant) as db:
        dept = await db.get(m.Department, personal)
        assert dept is not None
        dept.pending_frame_tools = {
            KEY: {**FRAME["tools"][KEY], "default_connection_id": str(gone)}
        }

    await _consolidate(app_session, tenant)

    async with app_session(tenant) as db:
        dept = await db.get(m.Department, personal)
        assert dept is not None and dept.pending_frame_tools is not None
        assert dept.pending_frame_tools[KEY]["default_connection_id"] == str(keep)
        assert await db.get(m.McpConnection, gone) is None


async def test_pins_follow_the_collapsed_row(app_session: AppSessionFactory) -> None:
    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    sales = await _department(app_session, tenant, "Sales")
    keep = await _row(app_session, tenant, department_id=agent.department_id, age=10)
    gone = await _row(app_session, tenant, department_id=sales, age=5)
    pinned = {"tools": {KEY: {**NARROWING["tools"][KEY], "connection_id": str(gone)}}}
    async with app_session(tenant) as db:
        live = await db.get(m.Agent, agent.id)
        assert live is not None and live.current_version_id is not None
        live.narrowing = pinned
        version = await db.get(m.AgentVersion, live.current_version_id)
        assert version is not None
        version.payload = {**version.payload, "narrowing": pinned}
        dept = await db.get(m.Department, sales)
        assert dept is not None
        dept.frame = {"tools": {KEY: {**FRAME["tools"][KEY], "default_connection_id": str(gone)}}}

    report = await _consolidate(app_session, tenant)

    async with app_session(tenant) as db:
        live = await db.get(m.Agent, agent.id)
        assert live is not None and live.current_version_id is not None
        assert live.narrowing["tools"][KEY]["connection_id"] == str(keep)
        version = await db.get(m.AgentVersion, live.current_version_id)
        assert version is not None
        assert version.payload["narrowing"]["tools"][KEY]["connection_id"] == str(keep)
        assert version.payload_hash == payload_hash(
            {k: v for k, v in version.payload.items() if k != META_KEY}
        )
        dept = await db.get(m.Department, sales)
        assert dept is not None
        assert dept.frame["tools"][KEY]["default_connection_id"] == str(keep)
        assert await db.get(m.McpConnection, gone) is None
    assert report.pins_rewritten == 3


async def test_a_login_row_becomes_a_fixed_choice(app_session: AppSessionFactory) -> None:
    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    async with app_session(tenant) as db:
        cred = m.Credential(
            tenant_id=tenant,
            name="Bot",
            credential_type=TYPE,
            field_values={"url": "https://bot.test"},
            secret_refs={},
        )
        db.add(cred)
        await db.flush()
        cred_id = cred.id
    shared = await _row(app_session, tenant, department_id=None, url=None, token_ref=None, age=20)
    login = await _row(
        app_session, tenant, department_id=None, url=None, token_ref=None, credential_id=cred_id
    )
    async with app_session(tenant) as db:
        live = await db.get(m.Agent, agent.id)
        assert live is not None and live.current_version_id is not None
        live.narrowing = {"tools": {KEY: {**NARROWING["tools"][KEY], "connection_id": str(login)}}}
        # Published: what the agent's runs execute.
        version = await db.get(m.AgentVersion, live.current_version_id)
        assert version is not None
        version.payload = {**version.payload, "narrowing": live.narrowing}

    report = await _consolidate(app_session, tenant)

    async with app_session(tenant) as db:
        access = await db.scalar(
            select(m.AgentToolAccess).where(m.AgentToolAccess.agent_id == agent.id)
        )
        assert access is not None
        assert (access.tool_key, access.mode, access.credential_id) == (KEY, "fixed", cred_id)
        assert access.set_with_manage is True
        assert await db.get(m.McpConnection, login) is None
        live = await db.get(m.Agent, agent.id)
        assert live is not None
        assert live.narrowing["tools"][KEY]["connection_id"] == str(shared)
        stored = await db.get(m.Credential, cred_id)
        assert stored is not None and str(login) in (stored.consolidated_from or [])
        # The run uses that login.
        run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, source="manual", context={})
        db.add(run)
        await db.flush()
        conn = await db.get(m.McpConnection, shared)
        assert conn is not None
        resolved = await resolve_tool_access(db, run=run, agent=live, conn=conn)
        assert resolved.credential is not None and resolved.credential.id == cred_id
    assert (report.fixed, report.collapsed) == (1, 1)


async def test_consolidation_is_idempotent(app_session: AppSessionFactory) -> None:
    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    sales = await _department(app_session, tenant, "Sales")
    await _row(app_session, tenant, department_id=agent.department_id, age=3)
    await _row(app_session, tenant, department_id=sales, url="https://b.test")
    await _consolidate(app_session, tenant)
    creds = [(c.id, c.consolidated_from) for c in await _creds(app_session, tenant)]
    rows = [r.id for r in await _rows(app_session, tenant)]

    again = await _consolidate(app_session, tenant)

    assert not again.changed
    assert (again.created, again.enabled, again.collapsed, again.pins_rewritten) == (0, 0, 0, 0)
    assert again.restamped == 0
    assert [(c.id, c.consolidated_from) for c in await _creds(app_session, tenant)] == creds
    assert [r.id for r in await _rows(app_session, tenant)] == rows


async def test_dry_run_changes_nothing(app_session: AppSessionFactory) -> None:
    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    sales = await _department(app_session, tenant, "Sales")
    await _row(app_session, tenant, department_id=agent.department_id, age=3)
    await _row(app_session, tenant, department_id=sales)

    report = await _consolidate(app_session, tenant, dry_run=True)

    assert report.dry_run and (report.created, report.collapsed) == (1, 1)
    assert await _creds(app_session, tenant) == []
    rows = await _rows(app_session, tenant)
    assert len(rows) == 2 and all(r.department_id is not None for r in rows)
    async with app_session(tenant) as db:
        enabled = await db.scalar(select(func.count()).select_from(m.CredentialDepartment))
        assert enabled == 0


# ------------------------------------------------------------------ confirmations


async def test_consolidation_keeps_confirmed_schedules_valid(
    app_session: AppSessionFactory,
) -> None:
    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    anna = await member(app_session, tenant, "anna@acme.test")
    await _row(app_session, tenant, department_id=agent.department_id)
    async with app_session(tenant) as db:
        own = m.Credential(
            tenant_id=tenant,
            name="Anna's",
            credential_type=TYPE,
            field_values={"url": "https://anna.test"},
            secret_refs={},
            owner_member_id=anna,
        )
        db.add(own)
        await db.flush()
        live = await db.get(m.Agent, agent.id)
        assert live is not None
        common = {
            "tenant_id": tenant,
            "agent_id": agent.id,
            "kind": "cron",
            "task_text": "report",
            "cron_expression": "0 9 * * *",
            "next_run_at": dt.datetime.now(dt.UTC),
            "created_by_member_id": anna,
        }
        confirmed = m.Trigger(**common)
        stale = m.Trigger(**common, access_fingerprint="stale")
        db.add_all([confirmed, stale])
        await db.flush()
        confirmed.access_fingerprint = await trigger_fingerprint(db, confirmed)
        access = m.AgentToolAccess(
            tenant_id=tenant,
            agent_id=agent.id,
            tool_key=KEY,
            mode="fixed",
            credential_id=own.id,
            set_by_member_id=anna,
        )
        db.add(access)
        await db.flush()
        access.binding_fingerprint = await binding_fingerprint(
            db, live, KEY, version_id=live.current_version_id
        )
        before = (confirmed.access_fingerprint, access.binding_fingerprint)
        ids = (confirmed.id, stale.id, access.id)

    report = await _consolidate(app_session, tenant)

    async with app_session(tenant) as db:
        after = await db.get(m.Trigger, ids[0])
        after_stale = await db.get(m.Trigger, ids[1])
        after_access = await db.get(m.AgentToolAccess, ids[2])
        live = await db.get(m.Agent, agent.id)
        assert after is not None and after_stale is not None and after_access is not None
        assert live is not None
        # The digest changed (the department owns no row any more) ...
        assert after.access_fingerprint != before[0]
        assert after_access.binding_fingerprint != before[1]
        # ... and was stamped again, so the schedule still runs.
        assert after.access_fingerprint == await trigger_fingerprint(db, after)
        assert after_access.binding_fingerprint == await binding_fingerprint(
            db, live, KEY, version_id=live.current_version_id
        )
        # A confirmation that was already invalid is not revived.
        assert after_stale.access_fingerprint == "stale"
    assert report.restamped == 2


@pytest.mark.parametrize("raised", [True, False])
async def test_a_split_confirmation_that_loosened_is_not_restamped(
    app_session: AppSessionFactory, raised: bool
) -> None:
    """Ruling T3-C2: a row with a split is judged by its split. Anna
    confirmed a threshold of 100; it was raised to 1000. The digest does not
    cover the threshold and is accepted -- the pass must not stamp the
    looser state as confirmed. Unchanged, it still holds when the pass
    collects (before folding) and is stamped again."""
    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    anna = await member(app_session, tenant, "anna@acme.test")
    await _row(app_session, tenant, department_id=agent.department_id)
    async with app_session(tenant) as db:
        own = m.Credential(
            tenant_id=tenant,
            name="Anna's",
            credential_type=TYPE,
            field_values={"url": "https://anna.test"},
            secret_refs={},
            owner_member_id=anna,
        )
        db.add(own)
        live = await db.get(m.Agent, agent.id)
        assert live is not None
        live.presentation = {**(live.presentation or {}), "approval_value_eur": 100}
        await db.flush()
        trigger = m.Trigger(
            tenant_id=tenant,
            agent_id=agent.id,
            kind="cron",
            task_text="report",
            cron_expression="0 9 * * *",
            next_run_at=dt.datetime.now(dt.UTC),
            created_by_member_id=anna,
        )
        access = m.AgentToolAccess(
            tenant_id=tenant,
            agent_id=agent.id,
            tool_key=KEY,
            mode="fixed",
            credential_id=own.id,
            set_by_member_id=anna,
        )
        db.add_all([trigger, access])
        await db.flush()
        await stamp_trigger(db, trigger, confirmed_by=anna)
        await stamp_binding(db, access, live, confirmed_by=anna)
        before = (
            trigger.access_fingerprint,
            trigger.access_split,
            access.binding_fingerprint,
            access.binding_split,
        )
        ids = (trigger.id, access.id, own.id)
        if raised:
            live.presentation = {**live.presentation, "approval_value_eur": 1000}

    report = await _consolidate(app_session, tenant)

    async with app_session(tenant) as db:
        after = await db.get(m.Trigger, ids[0])
        after_access = await db.get(m.AgentToolAccess, ids[1])
        live = await db.get(m.Agent, agent.id)
        assert after is not None and after_access is not None and live is not None
        stamps = (
            after.access_fingerprint,
            after.access_split,
            after_access.binding_fingerprint,
            after_access.binding_split,
        )
        reason = await unconfirmed_reason(
            db, after_access, live, KEY, ids[2], anna, version_id=live.current_version_id
        )
        if raised:
            assert stamps == before
            assert not await trigger_confirmed(db, after)
            assert reason == "agent_changed"
        else:
            assert stamps[0] != before[0] and stamps[2] != before[2]  # restamped
            assert await trigger_confirmed(db, after)
            assert reason is None
    assert report.restamped == (0 if raised else 2)


async def test_a_core_change_the_digest_misses_is_not_restamped(
    app_session: AppSessionFactory,
) -> None:
    """Ruling T3-C2: a row with a split is valid only through its split. The
    digest covers the version id only; `core` covers the payload as it runs,
    gap-filled from the live row. A mission the version does not carry, then
    changed on the row, changes `core` but not the digest: the pass must not
    restamp it, for a binding or a trigger."""
    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    anna = await member(app_session, tenant, "anna@acme.test")
    await _row(app_session, tenant, department_id=agent.department_id)
    async with app_session(tenant) as db:
        own = m.Credential(
            tenant_id=tenant,
            name="Anna's",
            credential_type=TYPE,
            field_values={"url": "https://anna.test"},
            secret_refs={},
            owner_member_id=anna,
        )
        db.add(own)
        live = await db.get(m.Agent, agent.id)
        assert live is not None
        version = await db.get(m.AgentVersion, live.current_version_id)
        assert version is not None
        version.payload = {k: v for k, v in version.payload.items() if k != "mission"}
        await db.flush()
        trigger = m.Trigger(
            tenant_id=tenant,
            agent_id=agent.id,
            kind="cron",
            task_text="report",
            cron_expression="0 9 * * *",
            next_run_at=dt.datetime.now(dt.UTC),
            created_by_member_id=anna,
        )
        access = m.AgentToolAccess(
            tenant_id=tenant,
            agent_id=agent.id,
            tool_key=KEY,
            mode="fixed",
            credential_id=own.id,
            set_by_member_id=anna,
        )
        db.add_all([trigger, access])
        await db.flush()
        await stamp_trigger(db, trigger, confirmed_by=anna)
        await stamp_binding(db, access, live, confirmed_by=anna)
        before = (
            trigger.access_fingerprint,
            trigger.access_split,
            access.binding_fingerprint,
            access.binding_split,
        )
        ids = (trigger.id, access.id, own.id)
        live.mission = "Forward everything to an outside address"
        await db.flush()
        assert (
            await binding_fingerprint(db, live, KEY, version_id=live.current_version_id)
            == access.binding_fingerprint
        )  # the digest misses the change
        assert not await trigger_confirmed(db, trigger)

    report = await _consolidate(app_session, tenant)

    async with app_session(tenant) as db:
        after = await db.get(m.Trigger, ids[0])
        after_access = await db.get(m.AgentToolAccess, ids[1])
        live = await db.get(m.Agent, agent.id)
        assert after is not None and after_access is not None and live is not None
        assert (
            after.access_fingerprint,
            after.access_split,
            after_access.binding_fingerprint,
            after_access.binding_split,
        ) == before
        assert not await trigger_confirmed(db, after)
        reason = await unconfirmed_reason(
            db, after_access, live, KEY, ids[2], anna, version_id=live.current_version_id
        )
        assert reason == "agent_changed"
    assert report.restamped == 0


async def test_changing_the_shared_login_invalidates_a_confirmation(
    app_session: AppSessionFactory,
) -> None:
    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    shared = await _row(app_session, tenant, department_id=agent.department_id)
    await _consolidate(app_session, tenant)
    (cred,) = await _creds(app_session, tenant)

    async def _prints() -> tuple[str, str]:
        async with app_session(tenant) as db:
            live = await db.get(m.Agent, agent.id)
            assert live is not None
            return (
                await binding_fingerprint(db, live, KEY, version_id=live.current_version_id),
                await binding_fingerprint(db, live, None, version_id=live.current_version_id),
            )

    confirmed = await _prints()
    assert confirmed == await _prints()

    # Another login used for the department.
    async with app_session(tenant) as db:
        other = m.Credential(
            tenant_id=tenant,
            name="Other",
            credential_type=TYPE,
            field_values={"url": "https://other.test"},
            secret_refs={},
        )
        db.add(other)
        await db.flush()
        stored = await db.get(m.Credential, cred.id)
        assert stored is not None
        await set_departments(db, stored, department_ids=set(), all_departments=False)
        await set_departments(
            db, other, department_ids={agent.department_id}, all_departments=False
        )
    swapped = await _prints()
    assert swapped[0] != confirmed[0] and swapped[1] != confirmed[1]

    # The shared row itself changed.
    async with app_session(tenant) as db:
        row = await db.get(m.McpConnection, shared)
        assert row is not None
        row.config = {**row.config, "args": ["-m", "other_bridge"]}
    changed = await _prints()
    assert changed[0] != swapped[0] and changed[1] != swapped[1]


# ------------------------------------------------------------------ the old pin rule


async def test_hiring_with_a_login_tool_after_consolidation_needs_no_connection_pin(
    app_session: AppSessionFactory,
) -> None:
    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    async with app_session(tenant) as db:
        cred = m.Credential(
            tenant_id=tenant,
            name="Bot",
            credential_type=TYPE,
            field_values={"url": "https://bot.test"},
            secret_refs={},
        )
        db.add(cred)
        await db.flush()
        cred_id = cred.id
    await _row(app_session, tenant, department_id=agent.department_id, age=5)
    login = await _row(
        app_session, tenant, department_id=None, url=None, token_ref=None, credential_id=cred_id
    )
    async with app_session(tenant) as db:
        live = await db.get(m.Agent, agent.id)
        assert live is not None
        live.narrowing = {"tools": {KEY: {**NARROWING["tools"][KEY], "connection_id": str(login)}}}

    await _consolidate(app_session, tenant)

    async with api() as client:
        r = await client.post(
            "/api/v1/agents",
            json={"name": "Rep", "departmentId": str(agent.department_id), "narrowing": NARROWING},
            headers=headers(tenant, "op", role="org_admin"),
        )
        assert r.status_code == 201, r.text
        hired = uuid.UUID(r.json()["id"])
    async with app_session(tenant) as db:
        new = await db.get(m.Agent, hired)
        assert new is not None
        choice = await _resolve_mcp_connection(db, agent=new, run_context={})
        assert choice.error is None


async def test_a_remaining_login_row_accepts_an_enabled_login_instead_of_a_pin(
    app_session: AppSessionFactory,
) -> None:
    """The old rule (a login tool needs `connection_id`) only applies while
    the agent has no other way to its login."""
    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    async with app_session(tenant) as db:
        cred = m.Credential(
            tenant_id=tenant,
            name="Bot",
            credential_type=TYPE,
            field_values={"url": "https://bot.test"},
            secret_refs={},
        )
        db.add(cred)
        await db.flush()
        cred_id = cred.id
    await _row(
        app_session, tenant, department_id=None, url=None, token_ref=None, credential_id=cred_id
    )
    body = {"name": "Rep", "departmentId": str(agent.department_id), "narrowing": NARROWING}
    async with api() as client:
        refused = await client.post(
            "/api/v1/agents", json=body, headers=headers(tenant, "op", role="org_admin")
        )
        assert refused.status_code == 422, refused.text
        async with app_session(tenant) as db:
            stored = await db.get(m.Credential, cred_id)
            assert stored is not None
            await set_departments(
                db, stored, department_ids={agent.department_id}, all_departments=False
            )
        accepted = await client.post(
            "/api/v1/agents", json=body, headers=headers(tenant, "op", role="org_admin")
        )
        assert accepted.status_code == 201, accepted.text


# ------------------------------------------------------------------ worker start


async def test_a_failing_tenant_does_not_stop_the_others(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from oc8.connections import consolidate
    from oc8.triggers import scheduler

    broken = (await _tenant(app_session)).tenant_id
    agent = await _tenant(app_session)
    await _row(app_session, agent.tenant_id, department_id=agent.department_id)
    real = consolidate.consolidate_tenant

    async def _maybe_fail(db: Any, tenant_id: uuid.UUID, *, dry_run: bool = False) -> Any:
        if tenant_id == broken:
            raise RuntimeError("secret detail that must not be logged")
        return await real(db, tenant_id, dry_run=dry_run)

    async def _tenants() -> list[uuid.UUID]:
        return [broken, agent.tenant_id]

    monkeypatch.setattr(scheduler, "list_active_tenant_ids", _tenants)
    monkeypatch.setattr(consolidate, "consolidate_tenant", _maybe_fail)

    reports = await consolidate.consolidate_all_tenants()

    assert [r.tenant_id for r in reports] == [agent.tenant_id]
    assert reports[0].created == 1
    assert len(await _creds(app_session, agent.tenant_id)) == 1
    assert str(broken) in caplog.text
    assert "secret detail" not in caplog.text


# ------------------------------------------------------------------ review fixes


async def _login_cred(
    app_session: AppSessionFactory, tenant: uuid.UUID, name: str, url: str
) -> uuid.UUID:
    async with app_session(tenant) as db:
        cred = m.Credential(
            tenant_id=tenant,
            name=name,
            credential_type=TYPE,
            field_values={"url": url},
            secret_refs={},
        )
        db.add(cred)
        await db.flush()
        return cred.id


def _pinned(conn_id: uuid.UUID) -> dict[str, Any]:
    return {"tools": {KEY: {**NARROWING["tools"][KEY], "connection_id": str(conn_id)}}}


async def test_a_published_login_pin_becomes_the_fixed_access_not_the_draft(
    app_session: AppSessionFactory,
) -> None:
    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    published = await _login_cred(app_session, tenant, "Published", "https://one.test")
    drafted = await _login_cred(app_session, tenant, "Drafted", "https://two.test")
    shared = await _row(app_session, tenant, department_id=None, url=None, token_ref=None, age=9)
    row_one = await _row(
        app_session, tenant, department_id=None, url=None, token_ref=None, credential_id=published
    )
    row_two = await _row(
        app_session, tenant, department_id=None, url=None, token_ref=None, credential_id=drafted
    )
    async with app_session(tenant) as db:
        live = await db.get(m.Agent, agent.id)
        assert live is not None and live.current_version_id is not None
        version = await db.get(m.AgentVersion, live.current_version_id)
        assert version is not None
        version.payload = {**version.payload, "narrowing": _pinned(row_one)}
        live.narrowing = _pinned(row_two)

    report = await _consolidate(app_session, tenant)

    async with app_session(tenant) as db:
        access = await db.scalar(
            select(m.AgentToolAccess).where(m.AgentToolAccess.agent_id == agent.id)
        )
        assert access is not None and access.credential_id == published
        live = await db.get(m.Agent, agent.id)
        assert live is not None and live.current_version_id is not None
        assert live.narrowing["tools"][KEY]["connection_id"] == str(shared)
        version = await db.get(m.AgentVersion, live.current_version_id)
        assert version is not None
        assert version.payload["narrowing"]["tools"][KEY]["connection_id"] == str(shared)
    assert (report.fixed, report.draft_pins_ignored) == (1, 1)


async def test_a_waiting_run_survives_consolidation(app_session: AppSessionFactory) -> None:
    from oc8.connections.tool_connection import RETIRED_KEY

    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    sales = await _department(app_session, tenant, "Sales")
    keep = await _row(app_session, tenant, department_id=agent.department_id, age=10)
    named = await _row(app_session, tenant, department_id=sales, age=5)
    async with app_session(tenant) as db:
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            source="manual",
            state="waiting_for_input",
            context={"mcp_connection_id": str(named)},
        )
        db.add(run)
        await db.flush()
        run_id = run.id

    report = await _consolidate(app_session, tenant)

    assert (report.collapsed, report.retired) == (0, 1)
    async with app_session(tenant) as db:
        retired = await db.get(m.McpConnection, named)
        assert retired is not None and not retired.connected
        assert retired.config[RETIRED_KEY] == str(keep)
        assert retired.department_id is None
        live = await db.get(m.Agent, agent.id)
        assert live is not None
        choice = await _resolve_mcp_connection(
            db, agent=live, run_context={"mcp_connection_id": str(named)}
        )
        assert choice.error is None and choice.connection is not None
        assert choice.connection.id == keep
        finished = await db.get(m.AgentRun, run_id)
        assert finished is not None
        finished.state = "done"

    later = await _consolidate(app_session, tenant)

    assert later.purged == 1 and later.restamped == 0
    async with app_session(tenant) as db:
        assert await db.get(m.McpConnection, named) is None
        live = await db.get(m.Agent, agent.id)
        assert live is not None
        choice = await _resolve_mcp_connection(
            db, agent=live, run_context={"mcp_connection_id": str(named)}
        )
        assert choice.connection is not None and choice.connection.id == keep


async def _tool_conn(db: Any, who: m.Agent) -> m.McpConnection:
    """The row the agent's runs reach: its department's own while
    unconsolidated, else the tool's one row."""
    rows = list(
        (
            await db.execute(
                select(m.McpConnection).where(
                    m.McpConnection.name == KEY,
                    (m.McpConnection.department_id == who.department_id)
                    | m.McpConnection.department_id.is_(None),
                )
            )
        ).scalars()
    )
    own = [r for r in rows if r.department_id == who.department_id]
    conn = (own or rows)[0]
    return conn


async def _used_url(app_session: AppSessionFactory, who: m.Agent) -> str | None:
    """The URL the agent's run would act on (the login's, or -- while
    unconsolidated -- the department row's own copy)."""
    async with app_session(who.tenant_id) as db:
        live = await db.get(m.Agent, who.id)
        assert live is not None
        conn = await _tool_conn(db, live)
        run = m.AgentRun(tenant_id=who.tenant_id, agent_id=live.id, source="manual", context={})
        db.add(run)
        await db.flush()
        access = await resolve_tool_access(db, run=run, agent=live, conn=conn)
        if access.credential is not None:
            return str(access.credential.field_values.get("url"))
        assert access.kind == "legacy"
        return str(conn.config["env"].get("FOLD_URL"))


async def _refusal(app_session: AppSessionFactory, who: m.Agent) -> Any:
    from oc8.connections.access import ConnectionRequired
    from oc8.connections.runtime_env import run_mcp_env

    async with app_session(who.tenant_id) as db:
        live = await db.get(m.Agent, who.id)
        assert live is not None
        conn = await _tool_conn(db, live)
        run = m.AgentRun(tenant_id=who.tenant_id, agent_id=live.id, source="manual", context={})
        db.add(run)
        await db.flush()
        with pytest.raises(ConnectionRequired) as caught:
            await run_mcp_env(db, run=run, conn=conn, tool="look")
        return caught.value


async def test_a_department_keeps_its_own_login_over_the_single_org_login_on_deploy(
    app_session: AppSessionFactory,
) -> None:
    """Eng copied its own login into its setup; the tenant also has one
    organisation login. Eng keeps acting through its own account -- before
    consolidation (resolver) and after it -- and only Sales, which had none,
    gets the single organisation login."""
    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    only = await _login_cred(app_session, tenant, "The one", "https://only.test")
    sales = await _department(app_session, tenant, "Sales")
    async with app_session(tenant) as db:
        seller = m.Agent(tenant_id=tenant, department_id=sales, name="Seller")
        db.add(seller)
        await db.flush()
    await _row(app_session, tenant, department_id=agent.department_id, url="https://eng.test")

    assert await _used_url(app_session, agent) == "https://eng.test"

    report = await _consolidate(app_session, tenant)

    assert await _used_url(app_session, agent) == "https://eng.test"
    assert await _used_url(app_session, seller) == "https://only.test"
    assert await _enabled(app_session, tenant, only) == {sales}
    folded = [c for c in await _creds(app_session, tenant) if c.id != only]
    assert len(folded) == 1
    assert folded[0].field_values == {"url": "https://eng.test"}
    assert await _enabled(app_session, tenant, folded[0].id) == {agent.department_id}
    assert report.kept_single == 1


async def test_an_incomplete_department_copy_is_reported_not_replaced(
    app_session: AppSessionFactory,
) -> None:
    """A department copy without its URL is not swapped for the single
    complete organisation login: it stays the department's and is reported
    as incomplete, naming the field."""
    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    only = await _login_cred(app_session, tenant, "The one", "https://only.test")
    await _row(app_session, tenant, department_id=agent.department_id, url="")

    before = await _refusal(app_session, agent)
    assert (before.reason, before.missing) == ("incomplete", ["url"])

    await _consolidate(app_session, tenant)

    after = await _refusal(app_session, agent)
    assert (after.reason, after.missing) == ("incomplete", ["url"])
    assert await _enabled(app_session, tenant, only) == set()


async def test_a_second_pass_does_not_revive_an_invalidated_confirmation(
    app_session: AppSessionFactory,
) -> None:
    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    await _row(app_session, tenant, department_id=agent.department_id)
    anna = await member(app_session, tenant, "anna@acme.test")
    await _consolidate(app_session, tenant)
    async with app_session(tenant) as db:
        live = await db.get(m.Agent, agent.id)
        assert live is not None
        legacy = await binding_fingerprint(
            db, live, None, version_id=live.current_version_id, legacy=True
        )
        trigger = m.Trigger(
            tenant_id=tenant,
            agent_id=agent.id,
            kind="cron",
            task_text="report",
            cron_expression="0 9 * * *",
            next_run_at=dt.datetime.now(dt.UTC),
            created_by_member_id=anna,
            # Stamped in the old form, no longer what the agent reaches.
            access_fingerprint=legacy,
        )
        db.add(trigger)
        await db.flush()
        assert legacy != await trigger_fingerprint(db, trigger)
        trigger_id = trigger.id
    # A later pass folds another department's new row.
    sales = await _department(app_session, tenant, "Sales")
    await _row(app_session, tenant, department_id=sales, url="https://b.test")

    again = await _consolidate(app_session, tenant)

    assert again.created == 1
    async with app_session(tenant) as db:
        stored = await db.get(m.Trigger, trigger_id)
        assert stored is not None and stored.access_fingerprint == legacy


async def test_a_pass_that_consolidates_nothing_restamps_nothing(
    app_session: AppSessionFactory,
) -> None:
    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    anna = await member(app_session, tenant, "anna@acme.test")
    # A shared row only: nothing to fold, but the digest has a shared part.
    await _row(app_session, tenant, department_id=None, url=None, token_ref=None)
    async with app_session(tenant) as db:
        live = await db.get(m.Agent, agent.id)
        assert live is not None
        legacy = await binding_fingerprint(
            db, live, None, version_id=live.current_version_id, legacy=True
        )
        trigger = m.Trigger(
            tenant_id=tenant,
            agent_id=agent.id,
            kind="cron",
            task_text="report",
            cron_expression="0 9 * * *",
            next_run_at=dt.datetime.now(dt.UTC),
            created_by_member_id=anna,
            access_fingerprint=legacy,
        )
        db.add(trigger)
        await db.flush()
        trigger_id = trigger.id

    report = await _consolidate(app_session, tenant)

    assert not report.changed and report.restamped == 0
    async with app_session(tenant) as db:
        stored = await db.get(m.Trigger, trigger_id)
        assert stored is not None and stored.access_fingerprint == legacy


# ------------------------------------------------------------------ fix round 2


async def test_a_forged_redirect_marker_is_not_followed(app_session: AppSessionFactory) -> None:
    from oc8.connections.tool_connection import (
        FOLDED_KEY,
        RETIRED_KEY,
        connection_for_context,
    )

    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    tool = await _row(app_session, tenant, department_id=None, url=None, token_ref=None, age=30)
    other_dept_row = await _row(app_session, tenant, department_id=agent.department_id, age=20)
    gone = uuid.uuid4()
    async with app_session(tenant) as db:
        custom = m.McpConnection(
            tenant_id=tenant,
            name="custom_server",
            server_url="",
            transport="stdio",
            connected=True,
            # Claims to be retired into the login tool's row: another tool.
            config={"command": "x", RETIRED_KEY: str(tool)},
        )
        to_dept_row = m.McpConnection(
            tenant_id=tenant,
            name=KEY,
            server_url="",
            transport="stdio",
            connected=True,
            # Same name, but the successor is a department's row.
            config={**_config(), RETIRED_KEY: str(other_dept_row)},
        )
        claims_fold = m.McpConnection(
            tenant_id=tenant,
            name=KEY,
            server_url="",
            transport="stdio",
            connected=True,
            # A younger shared row claiming a removed id: not the tool's row.
            config={**_config(), FOLDED_KEY: [str(gone)]},
        )
        db.add_all([custom, to_dept_row, claims_fold])
        await db.flush()
        ids = (custom.id, to_dept_row.id)

    async with app_session(tenant) as db:
        assert await connection_for_context(db, ids[0]) is None
        assert await connection_for_context(db, ids[1]) is None
        assert await connection_for_context(db, gone) is None
        live = await db.get(m.Agent, agent.id)
        assert live is not None
        choice = await _resolve_mcp_connection(
            db, agent=live, run_context={"mcp_connection_id": str(ids[0])}
        )
        assert choice.connection is None and choice.error is not None


async def test_users_cannot_write_reserved_consolidation_keys(
    app_session: AppSessionFactory,
) -> None:
    from types import SimpleNamespace

    from oc8.capas.materialise import unbound_connection
    from oc8.connections.tool_connection import FOLDED_KEY, RETIRED_KEY

    tenant = uuid.uuid4()
    # A manifest (a custom capa's) declaring the keys: dropped when its row
    # is materialised.
    manifest = SimpleNamespace(name="evil_pack")
    pack_conn = SimpleNamespace(
        key="primary",
        name="evil_tool",
        server_url="",
        transport="stdio",
        scopes=[],
        config={"command": "x", RETIRED_KEY: str(uuid.uuid4()), FOLDED_KEY: [str(uuid.uuid4())]},
    )
    row = unbound_connection(tenant_id=tenant, manifest=manifest, conn=pack_conn)  # type: ignore[arg-type]
    assert RETIRED_KEY not in row.config and FOLDED_KEY not in row.config
    assert row.config["command"] == "x"

    # The connection API has no way to set them either.
    admin = headers(tenant, "admin@acme.test", "org_admin")
    body = {
        "name": "custom_server",
        "command": "x",
        "env": {RETIRED_KEY: "y"},
        RETIRED_KEY: str(uuid.uuid4()),
        "config": {RETIRED_KEY: str(uuid.uuid4()), FOLDED_KEY: [str(uuid.uuid4())]},
    }
    async with api() as client:
        r = await client.post("/api/v1/mcp/connections", json=body, headers=admin)
        assert r.status_code in (201, 422), r.text
        created = r.json().get("id") if r.status_code == 201 else None
        r = await client.post(
            "/api/v1/mcp/connections",
            json={"name": "plain_server", "command": "x"},
            headers=admin,
        )
        assert r.status_code == 201, r.text
        plain = r.json()["id"]
        r = await client.patch(
            f"/api/v1/mcp/connections/{plain}",
            json={"command": "y", RETIRED_KEY: str(uuid.uuid4())},
            headers=admin,
        )
        assert r.status_code in (200, 422), r.text
    async with app_session(tenant) as db:
        for conn_id in (created, plain):
            if conn_id is None:
                continue
            stored = await db.get(m.McpConnection, uuid.UUID(conn_id))
            assert stored is not None
            assert RETIRED_KEY not in stored.config and FOLDED_KEY not in stored.config


async def test_a_department_default_login_survives_consolidation(
    app_session: AppSessionFactory,
) -> None:
    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    only = await _login_cred(app_session, tenant, "The one", "https://only.test")
    shared = await _row(app_session, tenant, department_id=None, url=None, token_ref=None, age=9)
    # The department's default names a row pinned to a second organisation
    # login: that one is what the department's agents used.
    other = await _login_cred(app_session, tenant, "Department's", "https://dept.test")
    login_row = await _row(
        app_session, tenant, department_id=None, url=None, token_ref=None, credential_id=other
    )
    sales = await _department(app_session, tenant, "Sales")
    async with app_session(tenant) as db:
        dept = await db.get(m.Department, agent.department_id)
        assert dept is not None
        dept.frame = {
            "tools": {KEY: {**FRAME["tools"][KEY], "default_connection_id": str(login_row)}}
        }

    report = await _consolidate(app_session, tenant)

    # Eng keeps its default's login (now enabled for it); with two
    # organisation logins no single one was in use, so Sales gets none.
    assert await _enabled(app_session, tenant, other) == {agent.department_id}
    assert await _enabled(app_session, tenant, only) == set()
    assert sales not in await _enabled(app_session, tenant, other)
    async with app_session(tenant) as db:
        live = await db.get(m.Agent, agent.id)
        assert live is not None
        newcomer = m.Agent(tenant_id=tenant, department_id=agent.department_id, name="New")
        db.add(newcomer)
        await db.flush()
        run = m.AgentRun(tenant_id=tenant, agent_id=newcomer.id, source="manual", context={})
        db.add(run)
        await db.flush()
        conn = await db.get(m.McpConnection, shared)
        assert conn is not None
        # An agent created after the pass gets the department's login too.
        access = await resolve_tool_access(db, run=run, agent=newcomer, conn=conn)
        assert access.credential is not None and access.credential.id == other
        dept = await db.get(m.Department, agent.department_id)
        assert dept is not None
        assert dept.frame["tools"][KEY]["default_connection_id"] == str(shared)
    assert report.kept_single == 0


async def test_a_department_default_on_the_single_login_keeps_it(
    app_session: AppSessionFactory,
) -> None:
    """The single organisation login, named by a department default through a
    login row: every department reaching the tool keeps it."""
    agent = await _tenant(app_session)
    tenant = agent.tenant_id
    only = await _login_cred(app_session, tenant, "The one", "https://only.test")
    await _row(app_session, tenant, department_id=None, url=None, token_ref=None, age=9)
    login_row = await _row(
        app_session, tenant, department_id=None, url=None, token_ref=None, credential_id=only
    )
    sales = await _department(app_session, tenant, "Sales")
    async with app_session(tenant) as db:
        dept = await db.get(m.Department, agent.department_id)
        assert dept is not None
        dept.frame = {
            "tools": {KEY: {**FRAME["tools"][KEY], "default_connection_id": str(login_row)}}
        }

    await _consolidate(app_session, tenant)

    assert await _enabled(app_session, tenant, only) == {agent.department_id, sales}
