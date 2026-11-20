"""The web UI's second gate on record links (rulings R9, R11).

`vet_answer_links` reads Markdown with regexes; the browser reads it with a
real parser. So the chat renderer asks the server about the hrefs its parser
produced for one saved answer: `POST /record-links/verify` says, per URL,
whether it points at a connected system and whether oc8 issued it. It is
bound to an answer the caller may read and to the URLs that answer contains,
so it is no oracle for constructed URLs.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.capas.manifest import RecordUrlTemplate, ToolPackConnection
from oc8.main import create_app
from oc8.records.registry import remember_links
from tests.conftest import AppSessionFactory
from tests.copilot.helpers import copilot_seat

pytestmark = pytest.mark.asyncio

ISSUED = "https://odoo.example.com/odoo/sale.order/42"
GUESSED = "https://odoo.example.com/odoo/sale.order/99"
BRACKETED = "https://odoo.example.com/odoo/action-1?ids=[1,2]#Größe"
REENCODED = "https://ODOO.example.com/odoo/action-1?ids=%5B1,2%5D#Gr%C3%B6%C3%9Fe"


def _h(tenant: uuid.UUID, subject: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role="org_admin")
    return {"Authorization": f"Bearer {token}"}


async def _verify_call(
    tenant: uuid.UUID,
    subject: str,
    message_id: uuid.UUID,
    urls: list[str],
    extra: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.post(
                "/api/v1/record-links/verify",
                json={"messageId": str(message_id), "urls": urls, **(extra or {})},
                headers=_h(tenant, subject),
            )
            return resp.status_code, resp.json()


async def _verdicts(
    tenant: uuid.UUID,
    message_id: uuid.UUID,
    urls: list[str],
    subject: str = "alice",
    extra: dict[str, Any] | None = None,
) -> dict[str, tuple[bool, bool]]:
    code, body = await _verify_call(tenant, subject, message_id, urls, extra)
    assert code == 200, body
    return {v["url"]: (v["connected"], v["issued"]) for v in body["links"]}


async def _answer(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    content: str,
    *,
    issued: list[str],
    role: str = "assistant",
) -> uuid.UUID:
    """Alice's chat with one message, and the links oc8 issued in that chat."""
    async with app_session(tenant) as db:
        seat = await copilot_seat(db, tenant, "alice")
        msg = m.ChatMessage(
            tenant_id=tenant, session_id=seat.session_id, role=role, content=content
        )
        db.add(msg)
        await remember_links(db, tenant_id=tenant, scope=f"chat:{seat.session_id}", urls=issued)
        await db.flush()
        return msg.id


async def test_judges_the_links_of_an_answer(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    content = f"[a]({ISSUED}) [b]({BRACKETED}) [c]({GUESSED}) [d](https://example.com/x)"
    msg = await _answer(app_session, tenant, content, issued=[ISSUED, BRACKETED])
    got = await _verdicts(tenant, msg, [ISSUED, REENCODED, GUESSED, "https://example.com/x"])
    assert got == {
        ISSUED: (True, True),
        # The browser re-encoded the href; it still leads where the issued one does.
        REENCODED: (True, True),
        GUESSED: (True, False),
        "https://example.com/x": (False, False),
    }


async def test_an_issued_url_the_answer_does_not_contain_is_never_issued(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    msg = await _answer(app_session, tenant, "No links here.", issued=[ISSUED, BRACKETED])
    got = await _verdicts(tenant, msg, [ISSUED, REENCODED])
    assert got == {ISSUED: (True, False), REENCODED: (True, False)}


async def test_backslash_and_port_spellings_match_by_target(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    backslash = "https://odoo.example.com\\odoo\\sale.order\\42"
    default_port = "https://odoo.example.com:443/odoo/sale.order/42"
    other_port = "https://odoo.example.com:8443/odoo/sale.order/42"
    plain_http = "http://odoo.example.com/odoo/sale.order/42"
    content = " ".join(f"[x]({u})" for u in (backslash, default_port, other_port, plain_http))
    msg = await _answer(app_session, tenant, content, issued=[ISSUED])
    got = await _verdicts(tenant, msg, [backslash, default_port, other_port, plain_http])
    assert got == {
        backslash: (True, True),
        default_port: (True, True),
        other_port: (True, False),
        plain_http: (True, False),
    }


async def test_another_tenants_or_an_expired_link_is_not_issued(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, other = uuid.uuid4(), uuid.uuid4()
    async with app_session(other) as db:
        await remember_links(db, tenant_id=other, scope="chat:x", urls=[GUESSED])
    msg = await _answer(app_session, tenant, f"[a]({ISSUED}) [b]({GUESSED})", issued=[ISSUED])
    await _connect_odoo(app_session, tenant, monkeypatch, "https://odoo.example.com")
    async with app_session(tenant) as db:
        await db.execute(
            update(m.RecordLink)
            .where(m.RecordLink.tenant_id == tenant)
            .values(issued_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=31))
        )
    got = await _verdicts(tenant, msg, [ISSUED, GUESSED])
    assert got == {ISSUED: (True, False), GUESSED: (True, False)}


async def test_a_link_issued_in_another_conversation_is_not_issued(
    app_session: AppSessionFactory,
) -> None:
    """Even quoted in a readable answer: the registry is no oracle for what
    agents touched in other members' chats or other departments."""
    tenant = uuid.uuid4()
    msg = await _answer(app_session, tenant, f"[a]({ISSUED}) [b]({GUESSED})", issued=[ISSUED])
    async with app_session(tenant) as db:
        for scope in (f"chat:{uuid.uuid4()}", f"task:{uuid.uuid4()}"):
            await remember_links(db, tenant_id=tenant, scope=scope, urls=[GUESSED])
    got = await _verdicts(tenant, msg, [ISSUED, GUESSED])
    assert got == {ISSUED: (True, True), GUESSED: (True, False)}


async def test_a_host_issued_only_in_another_conversation_is_not_connected(
    app_session: AppSessionFactory,
) -> None:
    """Ruling R22: without a GitHub connection, a github.com link another
    conversation was given does not make a library's repo link here a guess."""
    tenant = uuid.uuid4()
    repo = "https://github.com/psf/requests"
    msg = await _answer(app_session, tenant, f"[requests]({repo})", issued=[])
    async with app_session(tenant) as db:
        await remember_links(
            db,
            tenant_id=tenant,
            scope=f"chat:{uuid.uuid4()}",
            urls=["https://github.com/oc8-ai/oc8/issues/9"],
        )
    assert await _verdicts(tenant, msg, [repo]) == {repo: (False, False)}


async def _connect_odoo(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    odoo_url: str,
) -> None:
    """An Odoo connection whose base host is `odoo_url`'s."""
    template = RecordUrlTemplate(
        template="{base_url}/odoo/{model}/{id}",
        base_url_path=["env", "ODOO_URL"],
        models={"sale.order": "sale.order"},
    )
    conn = ToolPackConnection(key="primary", name="odoo", server_url="", record_url=template)
    monkeypatch.setattr(
        "oc8.capas.discovery.resolve_tool_pack_connection", lambda plugin, key: conn
    )
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Vertrieb")
        db.add(dept)
        await db.flush()
        db.add(
            m.McpConnection(
                tenant_id=tenant,
                department_id=dept.id,
                name="odoo",
                transport="stdio",
                server_url="stdio://odoo",
                connected=True,
                config={
                    "command": "x",
                    "args": [],
                    "env": {"ODOO_URL": odoo_url},
                    "_plugin_name": "odoo_mcp",
                    "_connection_key": "primary",
                },
            )
        )


async def test_a_connections_base_host_is_connected_before_any_link_was_issued(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    erp = "https://erp.example.com/odoo/sale.order/1"
    msg = await _answer(app_session, tenant, f"[a]({erp})", issued=[])
    await _connect_odoo(app_session, tenant, monkeypatch, "https://ERP.Example.com.")
    assert await _verdicts(tenant, msg, [erp]) == {erp: (True, False)}


async def test_a_guessed_link_to_an_ip_addressed_system_is_connected(
    app_session: AppSessionFactory,
) -> None:
    """A browser opens `3232235786` and `0xC0A8010A` as 192.168.1.10."""
    tenant = uuid.uuid4()
    decimal = "https://3232235786/odoo/sale.order/99"
    hexadecimal = "https://0xC0A8010A/odoo/sale.order/99"
    msg = await _answer(
        app_session,
        tenant,
        f"[a]({decimal}) [b]({hexadecimal})",
        issued=["https://192.168.1.10/odoo/sale.order/42"],
    )
    got = await _verdicts(tenant, msg, [decimal, hexadecimal])
    assert got == {decimal: (True, False), hexadecimal: (True, False)}


async def test_each_verdict_carries_the_servers_reading_of_the_host(
    app_session: AppSessionFactory,
) -> None:
    """The client compares it with the host it will open itself; where the
    two disagree the link stays text. A URL outside the answer reads `""`."""
    tenant = uuid.uuid4()
    decimal = "https://3232235786/odoo/sale.order/99"
    content = f"[a]({ISSUED}) [b]({decimal}) [c](https://Example.com./x)"
    msg = await _answer(app_session, tenant, content, issued=[ISSUED])
    code, body = await _verify_call(
        tenant, "alice", msg, [ISSUED, decimal, "https://Example.com./x", GUESSED]
    )
    assert code == 200, body
    assert {v["url"]: v["host"] for v in body["links"]} == {
        ISSUED: "odoo.example.com",
        decimal: "192.168.1.10",
        "https://Example.com./x": "example.com",
        GUESSED: "",
    }


async def test_a_host_named_by_the_client_is_not_used(app_session: AppSessionFactory) -> None:
    """Otherwise any link in an answer would test a guessed host for being
    connected -- `{url: <a docs link>, host: "erp.internal.corp"}`."""
    tenant = uuid.uuid4()
    docs = "https://example.com/docs"
    msg = await _answer(app_session, tenant, f"[docs]({docs})", issued=[ISSUED])
    sneaky = {"links": [{"url": docs, "host": "odoo.example.com"}]}
    assert await _verdicts(tenant, msg, [docs], extra=sneaky) == {docs: (False, False)}
    code, _body = await _verify_call(tenant, "alice", msg, [], extra={"urls": None, **sneaky})
    assert code == 422


async def test_a_url_the_answer_does_not_contain_says_nothing_about_its_host(
    app_session: AppSessionFactory,
) -> None:
    """Otherwise any readable answer would be a host oracle: submit URLs on
    guessed hosts and see which come back connected. A URL not in the answer
    is always `connected, not issued` -- rendered as text -- whatever its host."""
    tenant = uuid.uuid4()
    msg = await _answer(app_session, tenant, "Nothing linked.", issued=[ISSUED])
    other = "https://other.example/x"
    got = await _verdicts(tenant, msg, [GUESSED, other])
    assert got == {GUESSED: (True, False), other: (True, False)}


async def test_a_strict_prefix_of_an_answer_url_is_not_in_the_answer(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    longer = f"{ISSUED}0"  # .../sale.order/420
    msg = await _answer(app_session, tenant, f"See [S420]({longer}).", issued=[ISSUED, longer])
    got = await _verdicts(tenant, msg, [ISSUED, longer])
    assert got == {ISSUED: (True, False), longer: (True, True)}


async def test_adjacent_links_and_sentence_punctuation_still_verify(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    content = f"[a]({ISSUED})[b]({BRACKETED}), see {ISSUED}."
    msg = await _answer(app_session, tenant, content, issued=[ISSUED, BRACKETED])
    got = await _verdicts(tenant, msg, [ISSUED, BRACKETED, REENCODED])
    assert got == {ISSUED: (True, True), BRACKETED: (True, True), REENCODED: (True, True)}


async def test_another_members_answer_is_not_found(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    msg = await _answer(app_session, tenant, f"[a]({ISSUED})", issued=[ISSUED])
    async with app_session(tenant) as db:
        await copilot_seat(db, tenant, "bob")
    code, _body = await _verify_call(tenant, "bob", msg, [ISSUED])
    assert code == 404


async def test_a_members_own_message_is_not_judged(app_session: AppSessionFactory) -> None:
    """A member chooses what their own message says, so it is no answer to
    check -- it would make the endpoint an oracle for typed-in URLs."""
    tenant = uuid.uuid4()
    msg = await _answer(app_session, tenant, f"[a]({ISSUED})", issued=[ISSUED], role="user")
    code, _body = await _verify_call(tenant, "alice", msg, [ISSUED])
    assert code == 404


async def test_there_is_no_host_listing() -> None:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get("/api/v1/record-links/hosts", headers=_h(uuid.uuid4(), "alice"))
    assert resp.status_code in (404, 405)


@pytest.mark.parametrize(
    "urls",
    [
        [f"https://o.example/{i}" for i in range(101)],
        ["https://o.example/" + "a" * 2001],
    ],
)
async def test_verify_refuses_more_than_its_limits(
    app_session: AppSessionFactory, urls: list[str]
) -> None:
    code, _body = await _verify_call(uuid.uuid4(), "alice", uuid.uuid4(), urls)
    assert code == 422
