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

import re
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
            (
                await db.execute(
                    select(m.ChatMessage)
                    .where(m.ChatMessage.session_id == session_id)
                    .order_by(m.ChatMessage.id.asc())
                )
            )
            .scalars()
            .all()
        )
        runs = (
            (await db.execute(select(m.AgentRun).where(m.AgentRun.tenant_id == tenant)))
            .scalars()
            .all()
        )

    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[0].content == "apiKey=[redacted]"
    assert messages[1].content == (
        "I cannot accept, request, or use credential values. Use the normal secret setup flow."
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
    "password: $DB_PASSWORD",
    "token=%API_TOKEN%",
    "api_key: {{ secrets.api_key }}",
    "passphrase: ${SSH_PASSPHRASE}",
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
    # clear placeholders under a strong key
    "password = <your password>",
    "password: ***",
    "secret: siehe Vault",
    "Passwort: erforderlich",
    "Authorization: required",
    # A counter or switch in code whose name merely contains a strong word:
    # the identifier's other words are all counter/switch words, the value a
    # bare small number or boolean.
    "secret_count = 3",
    "secret_count: 0",
    "password_attempts=5",
    "password_attempts=3",
    "max_password_length: 128",
    "max_password_length = 12",
    "MAX_PASSWORD_LENGTH = 64",
    "secretCount = 0.5",
    "SECRET_ENABLED = True",
    "password_required = True",
    "is_secret = false",
    '{"secret_count": 3}',
]
#: A credential value the person wrote: refused (and redacted).
_VALUES = [
    # A passphrase is several words: all of them, to the end of the line.
    "passphrase: correct horse battery9 staple",
    "Passphrase = Pferd Batterie7 Heftklammer",
    "GPG_PASSPHRASE: my dog Rex2019 and cat\nand the rest of the note",
    # A value that only looks like an environment reference.
    "password=$Sommer2024",
    "password=%Sommer2024%",
    "secret={{ hunter2 }}",
    # A reference glued to a real value is the value.
    "api_key=${x}sk-realsecret123456",
    "password={{ x }}hunter2",
    "password=<x>hunter2",
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
    "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEF\nAASCBKcwggSj",
    "connect to postgres://admin:hunter2@db.example.com/app",
    # a strong key takes no exemption a weak key gets
    "password=gpt-4o",
    "password: 12345678",
    "secret = manager",
    "password=changeme",
    "Kennwort: Sommer",
    # command-line flags
    "--password=x",
    "run it with --password hunter2",
    "--api-key abcd1234efgh",
    "--token abc123def456ghi",
    "curl -u admin:hunter2 https://api.example.com",
    # an Authorization header holds a credential whatever its shape
    "Authorization: Bearer abc",
    "Authorization: xyz",
    # No counter exemption: a plain strong key takes any value; a compound key
    # the strong word ends (or that names a pin, a key, a numbered password)
    # names the credential; a quoted, longer or non-numeric value is a value.
    "password=42",
    "password = True",
    "secret = 1",
    "passphrase: 7",
    "pw=1234",
    "DB_PASSWORD=1234",
    "client_secret=1234",
    "API_KEY=1234",
    "SECRET_KEY = 42",
    "secret_pin = 1234",
    'secret_count = "42"',
    "secret_count = 42abc",
    "secret_count = 0.12345",
    # A qualifier naming an account or a system is how a short PIN or
    # password is written: only counter/switch words exempt a key.
    "password_admin=1234",
    "SECRET_PROD = 42",
    "db_password=7",
    "secret_bob: 4711",
    "PASSWORD_PROD=42",
    "password_reset_enabled: false",
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


def test_a_value_written_as_a_sentence_is_redacted() -> None:
    from oc8.copilot.redaction import redact_text

    assert redact_text("my password is hunter2, thanks") == "my password is [redacted], thanks"


@pytest.mark.parametrize("message", _VALUES)
def test_redaction_removes_every_value_the_gate_refuses(message: str) -> None:
    """One detector for refusal and redaction: what the gate refuses is never
    redaction-blind (titles, context objects, the stored refused turn)."""
    from oc8.copilot.redaction import carries_secret_value, redact_text

    redacted = redact_text(message)
    assert "[redacted]" in redacted
    assert not carries_secret_value(redacted), redacted


@pytest.mark.parametrize(
    "message",
    [
        "password_2 = 42",
        "SECRET_FROM_RUN1 = 12345",
        "SECRET_FROM_RUN1 = '42'",
        # Refused by design (seen live): "from"/"run1" are no counter words,
        # and a refused prompt is cheap where a leaked PIN is not.
        "a variable SECRET_FROM_RUN1 = 42",
    ],
)
def test_a_counter_key_with_a_digit_still_refuses_a_value(message: str) -> None:
    """Kept out of `_VALUES`, whose trace check reads a key with a digit as a
    leftover value: a numbered password is the credential, and a counter
    takes no long or quoted value."""
    from oc8.copilot.redaction import carries_secret_value, redact_text

    assert carries_secret_value(message) is True
    redacted = redact_text(message)
    assert "[redacted]" in redacted
    assert not carries_secret_value(redacted), redacted


@pytest.mark.parametrize("message", _MENTIONS)
def test_redaction_leaves_a_mention_alone(message: str) -> None:
    from oc8.copilot.redaction import redact_text

    assert redact_text(message) == message


@pytest.mark.parametrize(
    ("text", "refused"),
    [
        ("Explain our password policy", False),
        ("Answer questions about token usage: max token: 8000 per run.", False),
        ("Use DB_PASSWORD=Sommer2024 for the reports.", True),
        ("Nutze Passwort: Sommer2024 für Odoo.", True),
        ("Nutze password=hunter2 für Odoo.", True),
        ("narrowing password=x", True),
        ("Bearer abc.def", True),
        ("token=abc123", True),
        ("api_key=sk-123", True),
    ],
)
def test_stored_configuration_refuses_only_values(text: str, refused: bool) -> None:
    """Project instructions and builder proposals read the same detector: a
    policy text passes, a value does not (ea53133b; the builder's leaf texts
    turn `{"password": "x"}` into `password=x`)."""
    from oc8.copilot.redaction import carries_secret_value

    assert carries_secret_value(text) is refused


_ADVERSARIAL = {
    "dots": ("a.", 500_000),
    "dashes": ("a-", 500_000),
    "words": ("password", 100_000),
    "separators": ("=", 1_000_000),
    "quotes": ('"', 1_000_000),
    "quoted-keys": ("'password': '", 80_000),
    "flags": ("--password ", 90_000),
    "weak-keys": ("token: ", 140_000),
    "headers": ("Authorization: Bearer ", 45_000),
    "tags": ("<a>", 330_000),
    "urls": ("a://", 250_000),
    "prose": ("my password is ", 70_000),
    "pem-begins": ("-----BEGIN PRIVATE KEY-----\n", 50_000),
    "angle-values": ("password=<", 200_000),
    "angle-values-spaced": ("password=< ", 100_000),
    "tags-open": ("<a>x", 250_000),
}


@pytest.mark.parametrize("name", [*_ADVERSARIAL, "jwt-run", "prose-blanks", "prose-colon-blanks"])
def test_the_detector_stays_linear_on_a_long_message(name: str) -> None:
    """About 1 MB of adversarial input in well under a second of CPU: no form
    backtracks quadratically (a quadratic one takes minutes here). CPU time,
    with the collector off, so a busy machine does not fail the test."""
    import gc
    import time

    from oc8.copilot.redaction import carries_secret_value

    if name == "jwt-run":
        text = "eyJ" + "a" * 1_000_000
    elif name == "prose-blanks":
        text = "password is" + " " * 1_000_000 + ";"
    elif name == "prose-colon-blanks":
        text = "password is" + " " * 500_000 + ":" + " " * 500_000 + ";"
    else:
        unit, times = _ADVERSARIAL[name]
        text = unit * times
    gc.disable()
    try:
        started = time.process_time()
        carries_secret_value(text + " password=hunter2")
        spent = time.process_time() - started
    finally:
        gc.enable()
    assert spent < 1.5, spent


def test_a_value_far_into_a_long_message_is_still_found() -> None:
    from oc8.copilot.redaction import carries_secret_value

    assert carries_secret_value("a" * 50_000 + " password=hunter2")


@pytest.mark.parametrize("lead", [0, 4000, 16_000])
def test_a_long_token_is_redacted_whole(lead: int) -> None:
    """A 5 000-character JWT and a 600-character quoted value are redacted to
    the last character, wherever in the message they stand (the earlier
    windowed scan split them at 4 096 / 16 384)."""
    from oc8.copilot.redaction import redact_text

    jwt = "eyJ" + "a" * 2500 + "." + "b" * 2000 + "." + "c" * 497
    quoted = "Z9" * 300
    text = "x " * lead + f"token {jwt} and " + '{"client_secret": "' + quoted + '"}'
    redacted = redact_text(text)
    assert "aaaa" not in redacted and "bbbb" not in redacted and "cccc" not in redacted
    assert "Z9" not in redacted


def test_a_long_key_and_a_value_on_the_next_line_are_read() -> None:
    from oc8.copilot.redaction import carries_secret_value

    assert carries_secret_value("X" * 100 + "_PASSWORD=hunter2")
    assert carries_secret_value("password:\n    hunter2")
    assert carries_secret_value("password      =      hunter2")
    assert carries_secret_value("password=" + "v" * 2000)


def test_a_pem_key_is_redacted_to_its_end_line() -> None:
    from oc8.copilot.redaction import redact_text

    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
    assert redact_text(f"key: {pem} done") == "key: [redacted] done"
    two = f"{pem}\nand\n{pem}"
    assert redact_text(two) == "[redacted]\nand\n[redacted]"


def test_a_long_angle_value_is_a_value() -> None:
    from oc8.copilot.redaction import carries_secret_value

    assert not carries_secret_value("password = <your password>")
    assert carries_secret_value("password = <" + "z9" * 100 + ">")
    assert carries_secret_value("password=<hunter2>")
    assert carries_secret_value("password=[hunter2]")
    assert not carries_secret_value("password=[redacted]")


def test_a_flag_takes_its_value() -> None:
    from oc8.copilot.redaction import carries_secret_value, redact_text

    assert redact_text("--password=-x9abc") == "--password=[redacted]"
    assert redact_text("--password x9-abc") == "--password [redacted]"
    assert not carries_secret_value("--password --verbose")
    assert carries_secret_value('{"auth": "dXNlcjpwYXNz"}')
    assert not carries_secret_value("auth: required")


#: Keys as people write them: the word, a prefix, a suffix, casing, quotes.
_GEN_KEYS = [
    "password", "passwd", "pwd", "secret", "api_key", "apiKey", "Passwort", "Kennwort",
    "credential", "client_secret", "access_key", "private_key",
    "token", "access_token", "refresh_token", "auth",
]  # fmt: skip
_GEN_VARIANTS = ["{k}", "{K}", "DB_{K}", "{k}_value", '"{k}"', "'{k}'", "app.{k}", "x-{k}"]
_GEN_SEPARATORS = ["=", ": ", " = ", " : ", "=>", " -> ", ":\n  ", "  =  "]
_GEN_VALUES = [
    "hunter2xy", "Sommer2024!", "s3cr3t-v4lue", "dXNlcjpwYXNzd29yZA==",
    "ghp_abcdefghijklmnopqrstuvwxyz0123", "Zx9#kLm2Qp", "correct-horse-battery-1",
]  # fmt: skip


def _generated() -> list[str]:
    cases = []
    for key in _GEN_KEYS:
        for variant in _GEN_VARIANTS:
            rendered = variant.format(k=key, K=key.upper())
            for separator in _GEN_SEPARATORS:
                for value in _GEN_VALUES:
                    quote = rendered[0] if rendered[0] in "\"'" else ""
                    cases.append(f"{rendered}{separator}{quote}{value}{quote}")
    return cases


def test_every_generated_assignment_is_refused_and_redacted_whole() -> None:
    """Key word x casing/prefix/suffix/quotes x separator x real-looking value:
    each is refused and its value leaves no trace after redaction."""
    from oc8.copilot.redaction import carries_secret_value, redact_text

    missed = [
        (case, redact_text(case))
        for case in _generated()
        if not carries_secret_value(case)
        or any(value in redact_text(case) for value in _GEN_VALUES)
    ]
    assert not missed, missed[:10]


_GEN_MENTIONS = [
    "How many {k}s did we use?",
    "Where do I change the {k}?",
    "The {k} handling is described in the wiki.",
    "Wie setze ich {k} zurück?",
]


def test_generated_mentions_pass() -> None:
    from oc8.copilot.redaction import carries_secret_value

    leaked = [
        sentence.format(k=key)
        for key in _GEN_KEYS
        for sentence in _GEN_MENTIONS
        if carries_secret_value(sentence.format(k=key))
    ]
    assert not leaked, leaked


def test_a_truncated_pem_key_is_redacted_to_the_end() -> None:
    """No END line: the body after BEGIN is the key; redaction must not be
    weaker than the refusal (the refused turn, titles and context objects
    store the redacted text)."""
    from oc8.copilot.redaction import carries_secret_value, redact_text

    text = "here it is:\n-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEF\nAASCBKcwggSj"
    assert carries_secret_value(text)
    redacted = redact_text(text)
    assert redacted == "here it is:\n[redacted]"


@pytest.mark.parametrize("message", _VALUES)
def test_redaction_is_never_weaker_than_the_refusal(message: str) -> None:
    """Whatever made the gate refuse leaves no trace after redaction: no run of
    six or more value characters holding a digit survives (the value shapes
    of `_VALUES` all hold one; a key word such as `OPENAI_API_KEY` or
    `api03` is not a value)."""
    from oc8.copilot.redaction import redact_text

    redacted = redact_text(message)
    left = [
        run
        for run in re.findall(r"[A-Za-z0-9_+/=.-]{6,}", redacted)
        if re.search(r"\d", run) and run not in {"OPENAI_API_KEY"}
    ]
    assert not left, (message, redacted)


@pytest.mark.parametrize(
    "text",
    [
        "api_key=${OPENAI_API_KEY}",
        "api_key=$OPENAI_API_KEY",
        "password=%DB_PASSWORD%",
        "token: {{ api_key }}",
        "secret = <API_KEY>",
        "Set OPENAI_API_KEY=${OPENAI_API_KEY} in the environment.",
    ],
)
def test_an_environment_reference_is_a_placeholder(text: str) -> None:
    """Configuration that names where the secret lives (an env var, a
    template) is not the secret -- also in project instructions and builder
    text, which read the same detector."""
    from oc8.copilot.redaction import carries_secret_value, redact_text

    assert not carries_secret_value(text)
    assert redact_text(text) == text


@pytest.mark.parametrize(
    ("text", "refused"),
    [
        ("passphrase=correct horse", True),
        ("SSH_PASSPHRASE: Sommer2024", True),
        ("my PIN is 1234", False),
        ("PIN: 1234", False),
    ],
)
def test_a_passphrase_is_a_strong_key_and_a_pin_is_not(text: str, refused: bool) -> None:
    from oc8.copilot.redaction import carries_secret_value

    assert carries_secret_value(text) is refused


@pytest.mark.parametrize(
    ("text", "redacted"),
    [
        ("passphrase: correct horse battery", "passphrase: [redacted]"),
        ("Passphrase: richtiges Pferd Batterie.", "Passphrase: [redacted]."),
        ("passphrase=correct horse\nnext line stays", "passphrase=[redacted]\nnext line stays"),
        ('passphrase: "correct horse" here', 'passphrase: "[redacted]" here'),
        ("passphrase: ${SSH_PASSPHRASE} in the env", "passphrase: ${SSH_PASSPHRASE} in the env"),
    ],
)
def test_a_bare_passphrase_is_redacted_to_the_end_of_its_line(text: str, redacted: str) -> None:
    """A passphrase is words with blanks between them: stopping at the first
    blank left "horse battery" in titles and the stored refused turn."""
    from oc8.copilot.redaction import redact_text

    assert redact_text(text) == redacted
