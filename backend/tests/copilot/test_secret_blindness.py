"""Secret-blindness for the tenant Assistant's chat gate.

Retired 2026-08-31 together with the raw-completion Copilot chat path
(`oc8.copilot.chat`, deleted in Task 5 of the unified-assistant plan) --
`oc8.chat.service.send_message` now carries the same pre-model secret gate
that `respond_to_copilot_message` used to run, reusing the same
`oc8.copilot.redaction.carries_secret_value`/`redact_text` helpers. Scoped to
the tenant Assistant only; the other two tests below pin the scoping from
both directions -- an ordinary agent's chat is unaffected by the gate, and
a benign message to the Assistant itself is NOT refused.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from oc8.chat.service import create_session, send_message
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_assistant_refuses_a_message_that_looks_like_a_credential(
    app_session: AppSessionFactory,
) -> None:
    """The gate fires before any run is enqueued: no AgentRun is created, the
    refusal lands as the very next ChatMessage, and -- unlike the retired
    copilot/chat.py's own persistence, which this exists to preserve -- the
    raw credential is redacted in the STORED user turn too, not just kept
    out of the model/proposal/audit boundaries."""
    # A fresh tenant per test, not the shared `acme_tenant` fixture: the
    # `runs == []` assertion below is only true for a tenant nobody else has
    # written an AgentRun under (see test_chat.py's other tests, and the
    # original version of this file, for the same reasoning).
    tenant = uuid.uuid4()
    secret = "SENTINEL-COPILOT-SECRET-NEVER-PERSISTS"

    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        session = await create_session(
            db, tenant_id=tenant, agent_id=assistant.id, member_id=uuid.uuid4()
        )
        session_id = session.id
        _user_message, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message=f"apiKey={secret}",
            originating_operator="op",
        )

    assert run is None

    async with app_session(tenant) as db:
        messages = (
            await db.execute(
                select(m.ChatMessage)
                .where(m.ChatMessage.session_id == session_id)
                .order_by(m.ChatMessage.id.asc())
            )
        ).scalars().all()
        runs = (
            await db.execute(select(m.AgentRun).where(m.AgentRun.tenant_id == tenant))
        ).scalars().all()

    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[0].content == "apiKey=[redacted]"
    assert messages[1].content == (
        "I cannot accept, request, or use credential values. "
        "Use the normal secret setup flow."
    )
    assert runs == []
    assert secret not in messages[0].content
    assert secret not in messages[1].content


async def test_assistant_still_enqueues_a_run_for_a_benign_message(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """Pins the gate from the other side: a message to the SAME Assistant
    session that does not look like a credential is not refused. Without
    this, a broken `carries_secret_value` check that always returns True (or an
    accidentally-dropped conjunct that made the Assistant refuse
    everything) would still pass the test above and the ordinary-agent test
    below -- neither actually exercises the Assistant's non-refusal path."""
    # redis_url must be REQUESTED, not merely available: enqueue_run publishes
    # onto the run queue, which lazily binds to the testcontainer only when a
    # test in the current session has asked for it first (see test_chat.py).
    tenant = uuid.uuid4()

    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        session = await create_session(
            db, tenant_id=tenant, agent_id=assistant.id, member_id=uuid.uuid4()
        )
        _user_message, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message="Hello! How many tickets are open right now?",
            originating_operator="op",
        )

    assert run is not None
    assert run.source == "chat"


async def test_an_ordinary_agents_chat_is_not_gated(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """The identical message to a non-Assistant agent's chat session is NOT
    refused -- proves the gate is scoped to the Assistant, not a blanket
    chat restriction."""
    # redis_url must be REQUESTED, not merely available: enqueue_run publishes
    # onto the run queue, which lazily binds to the testcontainer only when a
    # test in the current session has asked for it first (see test_chat.py).
    tenant = uuid.uuid4()
    secret = "SENTINEL-COPILOT-SECRET-NEVER-PERSISTS"

    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Helpdesk", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Lennart")
        db.add(agent)
        await db.flush()
        session = await create_session(
            db, tenant_id=tenant, agent_id=agent.id, member_id=uuid.uuid4()
        )
        _user_message, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message=f"apiKey={secret}",
            originating_operator="op",
        )

    assert run is not None
    assert run.source == "chat"


#: Words about credentials, not credentials: a token is also what a model's
#: usage is counted in (Cost Center), and a person may ask about a password.
_MENTIONS = [
    "How many tokens did I use this month?",
    "Wie viele Tokens habe ich verbraucht?",
    "What is our token budget?",
    "reset my password please",
    "Bitte setz mein Passwort zurück.",
    "Explain our secret handling policy.",
    "The password is required for every login.",
    "Tokens: 5000",
    "token budget: 10000",
    "the secret is that…",
    "the secret is that we ship on Fridays",
    "token: 5000 per month",
    "max token: 8000",
    "token=gpt-4o",
    "secret: none",
    "password = required field",
    "credential: see vault",
    "the token is 4096 long",
    "the password is 8 characters long",
    "my token is 2 days old",
    "Bearer of bad news",
    "We sell bearer bonds.",
    "Das Passwort ist abgelaufen.",
    "Mein Kennwort funktioniert nicht.",
    "Wie lange ist ein Token gültig?",
]
#: A credential value the person wrote: refused (and redacted).
_VALUES = [
    "token: abc123def456",
    "password = hunter2",
    "Hier ist mein Token: abc123def456",
    "apiKey=sk-abc123def456",
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.e30.sig",
    "my password is hunter2",
    "Mein Token ist abc123def456",
    # compound and snake_case keys
    "OPENAI_API_KEY=sk-abc123",
    "DB_PASSWORD=Sommer2024",
    "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123",
    "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCY",
    "client_secret=s3cr3t-v4lue",
    "access_token=ya29.a0AfH6SMBx",
    "refresh_token: 1//0gLkXyZabc",
    # JSON and quoted keys
    '{"password": "hunter2"}',
    "{'token': 'abc'}",
    # Authorization headers
    "Authorization: Basic dXNlcjpwYXNzd29yZA==",
    "Authorization: Token 9944b09199c62bcf",
    # prose without a digit, other spellings
    "my password hunter",
    "my password Sommer!",
    "password is: hunter2",
    "password -> x",
    "<password>x</password>",
    # missed by both earlier gates
    "pw: hunter2",
    "PW=hunter2",
    "Passwort: Sommer2024",
    "Mein Passwort ist Sommer!",
    "Das Kennwort lautet Geheim42",
    "kennwort=geheim",
    "api key = abcd1234efgh",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
    "sk-abcdefghijklmnopqrstuvwx",
    "sk-ant-api03-abcdefghijklmnopqrst",
    "ghp_abcdefghijklmnopqrstuvwxyz0123",
    "github_pat_11ABCDEFG0123456789_abcdefghij",
    "AKIAIOSFODNN7EXAMPLE",
    "xoxb-1234567890-abcdefghij",
    "glpat-abcdefghijklmnopqrst",
    "AIzaSyA-abcdefghijklmnopqrstuvwxyz012345",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----",
    "connect to postgres://admin:hunter2@db.example.com/app",
]


@pytest.mark.parametrize("message", _MENTIONS)
def test_a_message_that_only_mentions_a_credential_passes_the_gate(message: str) -> None:
    from oc8.copilot.redaction import carries_secret_value

    assert carries_secret_value(message) is False


@pytest.mark.parametrize("message", _VALUES)
def test_a_message_that_carries_a_credential_value_is_caught(message: str) -> None:
    from oc8.copilot.redaction import carries_secret_value

    assert carries_secret_value(message) is True


@pytest.mark.parametrize(
    "message", ["How many tokens did I use this month?", "Wie viele Tokens habe ich verbraucht?"]
)
async def test_the_assistant_answers_a_question_about_token_usage(
    app_session: AppSessionFactory, redis_url: str, message: str
) -> None:
    """Asking how many tokens were spent is the Cost Center's question, not a
    credential: the turn is answered, not refused."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        session = await create_session(
            db, tenant_id=tenant, agent_id=assistant.id, member_id=uuid.uuid4()
        )
        user_message, run = await send_message(
            db, session=session, tenant_id=tenant, message=message, originating_operator="op"
        )

    assert run is not None and run.source == "chat"
    assert user_message.content == message
