"""Shared setup for the polled event source tests: an organisation with a capa
that declares a polled event and a login tool, an organisation login, an agent
fixed to that login, and a fake poller registered for the event."""

from __future__ import annotations

import base64
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.events.sources import PolledEvent, PollResult, register_poller
from oc8.secrets.service import store_secret
from tests.conftest import AppSessionFactory

CREDENTIAL_TYPE = "test_login"
SECRET = "pa55-hunter2-very-secret"
EVENT_TYPE = "item.arrived"
LABEL = "New item"
#: The settings the test capa's polled event declares (`TriggerDecl.config`).
CONFIG: list[dict[str, Any]] = [{"key": "folder", "label": "Folder", "default": "in"}]


def use_kek(monkeypatch: pytest.MonkeyPatch) -> None:
    """The secret store needs a KEK to keep the login's password."""
    from oc8 import config

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )


@dataclass
class Polled:
    tenant: uuid.UUID
    agent_id: uuid.UUID
    credential_id: uuid.UUID
    source: str
    tool_key: str
    event_type: str = EVENT_TYPE


@dataclass
class FakePoller:
    """Answers each poll with `respond(login, config, cursor)`; records calls."""

    respond: Callable[[dict[str, str], dict[str, Any], dict[str, Any] | None], PollResult]
    calls: list[tuple[dict[str, str], dict[str, Any], dict[str, Any] | None]] = field(
        default_factory=list
    )

    async def __call__(
        self, login: dict[str, str], config: dict[str, Any], cursor: dict[str, Any] | None
    ) -> PollResult:
        self.calls.append((dict(login), dict(config), cursor))
        return self.respond(login, config, cursor)


def event(n: int) -> PolledEvent:
    return PolledEvent(delivery_id=f"d-{n}", payload={"id": n, "folder": "in"}, summary=f"item {n}")


def baseline_then(
    events: Callable[[dict[str, Any]], list[PolledEvent]], *, position: int = 5
) -> Callable[[dict[str, str], dict[str, Any], dict[str, Any] | None], PollResult]:
    """A poller answer: a baseline at `position`, then `events(cursor)` and the
    next position after them."""

    def respond(
        _login: dict[str, str], _config: dict[str, Any], cursor: dict[str, Any] | None
    ) -> PollResult:
        if cursor is None:
            return PollResult(next_cursor={"pos": position}, events=[event(999)])
        found = events(cursor)
        return PollResult(next_cursor={"pos": int(cursor["pos"]) + len(found)}, events=found)

    return respond


async def organisation(app_session: AppSessionFactory, tenant: uuid.UUID) -> None:
    async with app_session(tenant) as db:
        db.add(
            m.Organization(
                id=tenant, slug=f"t-{tenant.hex[:12]}", name="Org", tier="standard", region="eu"
            )
        )


async def install_capa(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    source: str,
    tool_key: str,
    *,
    config: list[dict[str, Any]] | None = None,
    setup: dict[str, Any] | None = None,
) -> None:
    async with app_session(tenant) as db:
        capa = m.Capa(tenant_id=tenant, name=source, type="tool_pack")
        db.add(capa)
        await db.flush()
        manifest = {
            "name": source,
            "version": "1.0.0",
            "type": "tool_pack",
            "triggers": [
                "pushed.thing",
                {
                    "type": EVENT_TYPE,
                    "label": LABEL,
                    "polled": True,
                    "config": CONFIG if config is None else config,
                },
            ],
            "tool_pack": {
                "connections": [
                    {"name": tool_key, "server_url": "", "credential_type": CREDENTIAL_TYPE}
                ]
            },
        }
        if setup is not None:
            manifest["setup"] = setup
        version = m.CapaVersion(
            tenant_id=tenant, capa_id=capa.id, semver="1.0.0", manifest=manifest, artifact_hash=b"x"
        )
        db.add(version)
        await db.flush()
        capa.current_version_id = version.id
        db.add(
            m.CapaInstallation(
                tenant_id=tenant, capa_id=capa.id, version_id=version.id, status="enabled"
            )
        )


async def login(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    *,
    username: str = "anna",
    credential_type: str = CREDENTIAL_TYPE,
    owner: uuid.UUID | None = None,
    secret: str = SECRET,
) -> uuid.UUID:
    async with app_session(tenant) as db:
        cred = m.Credential(
            tenant_id=tenant,
            name=f"Login {uuid.uuid4().hex[:6]}",
            credential_type=credential_type,
            field_values={"host": "in.example.test", "username": username},
            secret_refs={},
            owner_member_id=owner,
        )
        db.add(cred)
        await db.flush()
        ref = f"credential/{cred.id}/password"
        await store_secret(db, tenant_id=tenant, name=ref, value=secret, kind="credential")
        cred.secret_refs = {"password": ref}
        return cred.id


async def agent(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    *,
    tool_key: str,
    credential_id: uuid.UUID | None,
    mode: str = "fixed",
) -> uuid.UUID:
    async with app_session(tenant) as db:
        row = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Listener")
        db.add(row)
        await db.flush()
        db.add(
            m.AgentToolAccess(
                tenant_id=tenant,
                agent_id=row.id,
                tool_key=tool_key,
                mode=mode,
                credential_id=credential_id if mode == "fixed" else None,
            )
        )
        return row.id


async def polled_setup(
    app_session: AppSessionFactory,
    *,
    tenant: uuid.UUID | None = None,
    source: str | None = None,
    username: str = "anna",
    secret: str = SECRET,
    config: list[dict[str, Any]] | None = None,
    setup: dict[str, Any] | None = None,
) -> Polled:
    """A fresh organisation (or `tenant`) with the capa `source` installed, one
    login and one agent fixed to it."""
    if tenant is None:
        tenant = uuid.uuid4()
        await organisation(app_session, tenant)
    source = source or f"src_{uuid.uuid4().hex[:10]}"
    tool_key = f"{source}_tools"
    async with app_session(tenant) as db:
        installed = await db.scalar(select(m.Capa.id).where(m.Capa.name == source))
    if installed is None:
        await install_capa(app_session, tenant, source, tool_key, config=config, setup=setup)
    credential_id = await login(app_session, tenant, username=username, secret=secret)
    agent_id = await agent(app_session, tenant, tool_key=tool_key, credential_id=credential_id)
    return Polled(tenant, agent_id, credential_id, source, tool_key)


def register(polled: Polled, fake: FakePoller) -> None:
    register_poller(polled.source, polled.event_type, fake, credential_type=CREDENTIAL_TYPE)


# --- a capa whose tools sign in through an OAuth connection ---------------------------

TOKEN = "eyJ-org-access-token-0123456789"
DELEGATED_SCOPE = "https://example.test/mail.read https://example.test/cal"
#: Setup fields of the OAuth test capa: a default mailbox and the mailboxes
#: the login may act as.
OAUTH_CONFIG: list[dict[str, Any]] = [
    {
        "key": "mailbox",
        "label": "Mailbox",
        "default_from": "default_mailbox",
        "allowed_from": "delegated_mailboxes",
        "acts_as": True,
    },
    {"key": "folder", "label": "Folder", "default": "in"},
]


async def install_oauth_capa(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    source: str,
    tool_key: str,
    *,
    provider: str = "google",
    config: list[dict[str, Any]] | None = None,
) -> None:
    async with app_session(tenant) as db:
        capa = m.Capa(tenant_id=tenant, name=source, type="tool_pack")
        db.add(capa)
        await db.flush()
        manifest = {
            "name": source,
            "version": "1.0.0",
            "type": "tool_pack",
            "triggers": [
                {
                    "type": EVENT_TYPE,
                    "label": LABEL,
                    "polled": True,
                    "config": OAUTH_CONFIG if config is None else config,
                }
            ],
            "tool_pack": {"connections": [{"name": tool_key, "server_url": ""}]},
            "setup": {
                "title": "Connect",
                "fields": [
                    {"key": "default_mailbox", "label": "Default", "required": False},
                    {"key": "delegated_mailboxes", "label": "Mailboxes", "required": False},
                ],
                "mcp": {
                    "name": tool_key,
                    "env_fields": {"TEST_DEFAULT_MAILBOX": "default_mailbox"},
                },
                "oauth_provision": {
                    "provider": provider,
                    "delegated_identities_field": "delegated_mailboxes",
                    "delegated_identities_env": "TEST_DELEGATED_MAILBOXES",
                    "delegated_token_env_prefix": "TEST_DELEGATED_TOKEN",
                    "delegated_scope": DELEGATED_SCOPE,
                },
            },
        }
        version = m.CapaVersion(
            tenant_id=tenant, capa_id=capa.id, semver="1.0.0", manifest=manifest, artifact_hash=b"x"
        )
        db.add(version)
        await db.flush()
        capa.current_version_id = version.id
        db.add(
            m.CapaInstallation(
                tenant_id=tenant, capa_id=capa.id, version_id=version.id, status="enabled"
            )
        )


async def oauth_login(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    *,
    provider: str = "google",
    grant_type: str = "service_account",
    owner: uuid.UUID | None = None,
    status: str = "active",
    account_label: str = "bot@project.iam.example.test",
    token: str = TOKEN,
) -> uuid.UUID:
    """An OAuth connection whose stored access token is still fresh."""
    from datetime import UTC, datetime, timedelta

    from oc8.oauth.tokens import access_ref

    async with app_session(tenant) as db:
        row = m.OAuthConnection(
            tenant_id=tenant,
            provider=provider,
            account_label=account_label,
            scopes=[],
            access_secret_ref="",
            status=status,
            client_source="tenant",
            grant_type=grant_type,
            azure_tenant_id="contoso.onmicrosoft.com" if provider == "microsoft" else None,
            owner_member_id=owner,
            expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
        )
        db.add(row)
        await db.flush()
        row.access_secret_ref = access_ref(row.id)
        await store_secret(
            db, tenant_id=tenant, name=access_ref(row.id), value=token, kind="oauth_token"
        )
        return row.id


async def setup_row(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    source: str,
    tool_key: str,
    *,
    oauth_connection_id: uuid.UUID,
    default_mailbox: str = "info@acme.test",
    delegated: str = "info@acme.test,sales@acme.test",
) -> uuid.UUID:
    """The capa's setup connection for every department, as a setup with an
    OAuth app leaves it."""
    env = {"TEST_DEFAULT_MAILBOX": default_mailbox}
    if delegated:
        env["TEST_DELEGATED_MAILBOXES"] = delegated
    async with app_session(tenant) as db:
        row = m.McpConnection(
            tenant_id=tenant,
            name=tool_key,
            server_url="",
            transport="stdio",
            config={
                "_plugin_name": source,
                "_connection_key": "default",
                "env": env,
                "oauth_connection_id": str(oauth_connection_id),
            },
        )
        db.add(row)
        await db.flush()
        return row.id


async def oauth_agent(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    *,
    tool_key: str,
    oauth_connection_id: uuid.UUID,
) -> uuid.UUID:
    async with app_session(tenant) as db:
        row = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Listener")
        db.add(row)
        await db.flush()
        db.add(
            m.AgentToolAccess(
                tenant_id=tenant,
                agent_id=row.id,
                tool_key=tool_key,
                mode="fixed",
                oauth_connection_id=oauth_connection_id,
            )
        )
        return row.id


@dataclass
class OAuthPolled:
    tenant: uuid.UUID
    agent_id: uuid.UUID
    connection_id: uuid.UUID
    setup_id: uuid.UUID
    source: str
    tool_key: str
    event_type: str = EVENT_TYPE


async def oauth_polled_setup(
    app_session: AppSessionFactory,
    *,
    provider: str = "google",
    grant_type: str = "service_account",
    config: list[dict[str, Any]] | None = None,
    delegated: str = "info@acme.test,sales@acme.test",
) -> OAuthPolled:
    tenant = uuid.uuid4()
    await organisation(app_session, tenant)
    source = f"oa_{uuid.uuid4().hex[:10]}"
    tool_key = f"{source}_tools"
    await install_oauth_capa(
        app_session, tenant, source, tool_key, provider=provider, config=config
    )
    conn = await oauth_login(app_session, tenant, provider=provider, grant_type=grant_type)
    setup_id = await setup_row(
        app_session, tenant, source, tool_key, oauth_connection_id=conn, delegated=delegated
    )
    agent_id = await oauth_agent(app_session, tenant, tool_key=tool_key, oauth_connection_id=conn)
    polled = OAuthPolled(tenant, agent_id, conn, setup_id, source, tool_key)
    # A poller that finds nothing, until a test registers its own.
    register_oauth(polled, FakePoller(baseline_then(lambda _c: [])))
    return polled


def register_oauth(polled: OAuthPolled, fake: FakePoller) -> None:
    register_poller(polled.source, polled.event_type, fake)


# --- a trigger on a login a department owner owns -------------------------------------

#: The personal frame of the owned world: the test capa's tools, read and write.
_OWNED_TOOLS: dict[str, Any] = {"enabled": True, "read": True, "modify": True}


@dataclass
class OwnedPolled:
    """A personal department's agent fixed to its owner's own login of the
    test capa, bound by the owner; `fake` answers its polls."""

    tenant: uuid.UUID
    member_id: uuid.UUID
    subject: str
    agent_id: uuid.UUID
    credential_id: uuid.UUID
    source: str
    tool_key: str
    fake: FakePoller
    event_type: str = EVENT_TYPE


async def bind_own_login(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    *,
    agent_id: uuid.UUID,
    member_id: uuid.UUID,
    tool_key: str,
    credential_id: uuid.UUID | None = None,
    oauth_connection_id: uuid.UUID | None = None,
) -> None:
    """The owner fixes the agent's `tool_key` to their own login: their
    confirmation of the binding."""
    from oc8.connections.access_write import write_tool_access

    async with app_session(tenant) as db:
        live = await db.get(m.Agent, agent_id)
        assert live is not None
        await write_tool_access(
            db,
            agent=live,
            tool_key=tool_key,
            mode="fixed",
            oauth_connection_id=oauth_connection_id,
            credential_id=credential_id,
            set_by_member_id=member_id,
            set_with_manage=False,
            actor_type="operator",
            actor_id=member_id,
        )


async def owned_polled_setup(
    app_session: AppSessionFactory,
    *,
    respond: Callable[[dict[str, str], dict[str, Any], dict[str, Any] | None], PollResult]
    | None = None,
) -> OwnedPolled:
    """`owned_world` with the test capa installed, the owner's own login of
    its type, the agent fixed to it by the owner and a fake poller
    registered (finding nothing unless `respond` says otherwise)."""
    from tests.departments.owned import owned_world

    source = f"own_{uuid.uuid4().hex[:10]}"
    tool_key = f"{source}_tools"
    world = await owned_world(app_session, tools={tool_key: dict(_OWNED_TOOLS)})
    assert world.agent_id is not None
    await install_capa(app_session, world.tenant, source, tool_key)
    credential_id = await login(app_session, world.tenant, owner=world.member_id)
    await bind_own_login(
        app_session,
        world.tenant,
        agent_id=world.agent_id,
        member_id=world.member_id,
        tool_key=tool_key,
        credential_id=credential_id,
    )
    async with app_session(world.tenant) as db:
        owner = await db.get(m.OrgMember, world.member_id)
        assert owner is not None
        subject = owner.subject
    fake = FakePoller(respond or baseline_then(lambda _c: []))
    register_poller(source, EVENT_TYPE, fake, credential_type=CREDENTIAL_TYPE)
    return OwnedPolled(
        world.tenant,
        world.member_id,
        subject,
        world.agent_id,
        credential_id,
        source,
        tool_key,
        fake,
    )
