"""The Copilot creates a trigger on a polled event with its settings; the
login it listens on is the agent's fixed login, never one the model names."""

from __future__ import annotations

import uuid
from typing import Any

import jsonschema
import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.control_tools import _BUILDER_OPERATION, PROPOSE_AGENT, PROPOSE_CHANGE
from oc8.copilot.builder_notice import REASONS
from oc8.copilot.builder_ops import (
    BuilderInvalid,
    builder_operation_data,
    flat_operations,
    parse_builder_operations,
)
from oc8.copilot.capabilities import InvalidOperation, apply_operation
from oc8.events.sources import PollResult
from tests.conftest import AppSessionFactory
from tests.events.polled import (
    EVENT_TYPE,
    FakePoller,
    Polled,
    agent,
    polled_setup,
    register,
    use_kek,
)

pytestmark = pytest.mark.asyncio


def _never(*_args: Any) -> PollResult:
    raise AssertionError("not polled in these tests")


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    use_kek(monkeypatch)


@pytest.fixture
async def polled(app_session: AppSessionFactory) -> Polled:
    setup = await polled_setup(app_session)
    register(setup, FakePoller(_never))
    return setup


def _operation(polled: Polled, agent_id: uuid.UUID, **extra: Any) -> dict[str, Any]:
    return {
        "type": "trigger.create",
        "agentId": str(agent_id),
        "kind": "event",
        "taskText": "Sort the mail",
        "eventSource": polled.source,
        "eventType": EVENT_TYPE,
        **extra,
    }


async def test_the_copilot_creates_a_trigger_on_the_agents_login(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    async with app_session(polled.tenant) as db:
        await apply_operation(
            db,
            tenant_id=polled.tenant,
            data=_operation(polled, polled.agent_id, eventConfig={"folder": "Orders"}),
        )

    async with app_session(polled.tenant) as db:
        trigger = await db.scalar(select(m.Trigger).where(m.Trigger.agent_id == polled.agent_id))
        assert trigger is not None
        assert trigger.event_target_credential_id == polled.credential_id
        assert trigger.event_config == {"folder": "Orders"}


async def test_the_copilot_is_refused_for_an_agent_without_a_login(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    no_login = await agent(
        app_session, polled.tenant, tool_key=polled.tool_key, credential_id=None, mode="user"
    )

    async with app_session(polled.tenant) as db:
        with pytest.raises(InvalidOperation) as caught:
            await apply_operation(db, tenant_id=polled.tenant, data=_operation(polled, no_login))
    assert caught.value.reason == "trigger_needs_login"
    assert "login" in REASONS["trigger_needs_login"]


async def test_the_copilot_is_refused_an_undeclared_setting(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    async with app_session(polled.tenant) as db:
        with pytest.raises(InvalidOperation) as caught:
            await apply_operation(
                db,
                tenant_id=polled.tenant,
                data=_operation(polled, polled.agent_id, eventConfig={"subject": "x"}),
            )
    assert caught.value.reason == "trigger_invalid"


def test_the_settings_are_short_strings() -> None:
    base = {
        "type": "trigger.create",
        "agentRef": "$new",
        "kind": "event",
        "taskText": "t",
        "eventSource": "s",
        "eventType": "e",
    }
    create = {"type": "agent.create", "ref": "$new", "departmentId": str(uuid.uuid4()), "name": "A"}
    ops = parse_builder_operations([create, {**base, "eventConfig": {"folder": "INBOX"}}])
    assert builder_operation_data(ops[1])["eventConfig"] == {"folder": "INBOX"}
    for bad in ({"folder": "x" * 201}, {"folder": 3}):
        with pytest.raises(BuilderInvalid):
            parse_builder_operations([create, {**base, "eventConfig": bad}])


def test_the_tools_offer_the_settings() -> None:
    properties = _BUILDER_OPERATION["properties"]
    assert isinstance(properties, dict) and "eventConfig" in properties
    assert "eventConfig" in PROPOSE_AGENT.parameters["properties"]
    assert "eventConfig" in PROPOSE_CHANGE.parameters["properties"]["payload"]["description"]
    flat = {
        "departmentId": str(uuid.uuid4()),
        "name": "Postfach",
        "mission": "Sortiert die Post.",
        "eventSource": "email",
        "eventType": "mail.received",
        "eventConfig": {"folder": "INBOX"},
        "taskText": "Sortier sie.",
    }
    jsonschema.Draft202012Validator(PROPOSE_AGENT.parameters).validate(flat)
    (trigger,) = [op for op in flat_operations(flat) if op["type"] == "trigger.create"]
    assert trigger["eventConfig"] == {"folder": "INBOX"}


def test_the_copilot_never_names_the_login() -> None:
    from oc8.copilot.capabilities import parse_operations

    operation = {
        "type": "trigger.create",
        "agentId": str(uuid.uuid4()),
        "kind": "event",
        "taskText": "t",
        "eventSource": "s",
        "eventType": "e",
        "eventTargetCredentialId": str(uuid.uuid4()),
    }
    with pytest.raises(InvalidOperation):
        parse_operations([operation])


def test_flat_settings_without_an_event_are_refused() -> None:
    flat = {
        "departmentId": str(uuid.uuid4()),
        "name": "Postfach",
        "mission": "Sortiert die Post.",
        "eventConfig": {"folder": "INBOX"},
    }
    with pytest.raises(BuilderInvalid) as caught:
        flat_operations(flat)
    assert caught.value.code == "event_config_without_event"


async def test_the_copilot_listens_on_an_oauth_login_and_keeps_to_the_list(
    app_session: AppSessionFactory,
) -> None:
    from tests.events.polled import oauth_polled_setup

    polled = await oauth_polled_setup(app_session)
    data = {
        "type": "trigger.create",
        "agentId": str(polled.agent_id),
        "kind": "event",
        "taskText": "Sort the mail",
        "eventSource": polled.source,
        "eventType": polled.event_type,
    }
    async with app_session(polled.tenant) as db:
        with pytest.raises(InvalidOperation):
            await apply_operation(
                db,
                tenant_id=polled.tenant,
                data={**data, "eventConfig": {"mailbox": "ceo@acme.test"}},
            )
    async with app_session(polled.tenant) as db:
        await apply_operation(
            db, tenant_id=polled.tenant, data={**data, "eventConfig": {"mailbox": "info@acme.test"}}
        )
    async with app_session(polled.tenant) as db:
        trigger = await db.scalar(select(m.Trigger).where(m.Trigger.agent_id == polled.agent_id))
        assert trigger is not None
        assert trigger.event_target_oauth_connection_id == polled.connection_id
        assert trigger.event_target_credential_id is None
        assert trigger.event_config["mailbox"] == "info@acme.test"
