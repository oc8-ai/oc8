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
