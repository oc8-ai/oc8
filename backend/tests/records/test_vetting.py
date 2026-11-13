from __future__ import annotations

import uuid

import pytest

from oc8.records.registry import remember_links
from oc8.records.vetting import vet_answer_links, vet_links
from tests.conftest import AppSessionFactory

HOSTS = {"odoo.example.com"}
GOOD = "https://odoo.example.com/odoo/sale.order/42"
BAD = "https://odoo.example.com/odoo/sale.order/99"


def test_an_issued_markdown_link_stays() -> None:
    text = f"See [S00042]({GOOD})."
    assert vet_links(text, connected_hosts=HOSTS, issued={GOOD}) == text


def test_an_unissued_markdown_link_keeps_its_text() -> None:
    assert vet_links(f"See [S00099]({BAD}).", connected_hosts=HOSTS, issued={GOOD}) == "See S00099."


def test_an_unissued_bare_link_becomes_its_host_as_code() -> None:
    assert (
        vet_links(f"Open {BAD} now", connected_hosts=HOSTS, issued=set())
        == "Open `odoo.example.com` now"
    )


def test_trailing_punctuation_is_not_part_of_the_url() -> None:
    text = f"Here: {GOOD}. And ({GOOD})"
    assert vet_links(text, connected_hosts=HOSTS, issued={GOOD}) == text


def test_only_unissued_links_to_connected_hosts_change() -> None:
    text = f"[run](oc8://run/1) [docs](https://docs.oc8.ai/x) [ok]({GOOD}) [no]({BAD})"
    assert vet_links(text, connected_hosts=HOSTS, issued={GOOD}) == (
        f"[run](oc8://run/1) [docs](https://docs.oc8.ai/x) [ok]({GOOD}) no"
    )


def test_a_guessed_url_inside_code_is_neutralised() -> None:
    out = vet_links(f"`{BAD}`\n```\n{BAD}\n```", connected_hosts=HOSTS, issued=set())
    assert BAD not in out
    assert out.count("`odoo.example.com`") >= 2


H = {"h.example"}
PAREN = "https://h.example/a_(b)/1"
BRACKET = "https://h.example/x?ids[]=1&a=b"
G = "https://h.example/odoo/99"


def test_issued_urls_with_brackets_survive() -> None:
    for text in (f"[t]({PAREN})", f"see {PAREN} now", f"see {BRACKET} now", f"({PAREN})"):
        assert vet_links(text, connected_hosts=H, issued={PAREN, BRACKET}) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"see ({G})", "see (`h.example`)"),
        (f"[Quote [draft] S1]({G}) ok", "Quote [draft] S1 ok"),
        (f"[t](<{G}>)", "t"),
        (f'[t]({G} "title") x', "t x"),
        (f"<{G}>", "<`h.example`>"),
        (f"[1]: {G}", "[1]: `h.example`"),
        (f'<a href="{G}">x</a>', '<a href="`h.example`">x</a>'),
        ("go www.h.example/odoo/x now", "go `www.h.example` now"),
    ],
)
def test_a_guessed_url_is_neutralised_whatever_wraps_it(text: str, expected: str) -> None:
    out = vet_links(text, connected_hosts={"h.example", "www.h.example"}, issued=set())
    assert out == expected


def test_other_schemes_and_hosts_are_untouched() -> None:
    text = "[run](oc8://run/1) [d](https://docs.oc8.ai/x)"
    assert vet_links(text, connected_hosts=H, issued=set()) == text


async def _boom(*_a: object, **_k: object) -> set[str]:
    raise RuntimeError("db down")


@pytest.mark.asyncio
async def test_failing_issued_lookup_fails_closed(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    monkeypatch.setattr("oc8.records.vetting.issued_links", _boom)
    async with app_session(tenant) as db:
        await remember_links(db, tenant_id=tenant, urls=[GOOD])
        out = await vet_answer_links(db, tenant_id=tenant, text=f"{GOOD} {BAD} https://o.org/x")
    assert GOOD not in out
    assert BAD not in out
    assert "https://o.org/x" in out


@pytest.mark.asyncio
async def test_failing_host_lookup_neutralises_every_link(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    monkeypatch.setattr("oc8.records.vetting.issued_links", _boom)
    monkeypatch.setattr("oc8.records.vetting.linked_hosts", _boom)
    async with app_session(tenant) as db:
        out = await vet_answer_links(db, tenant_id=tenant, text="a https://o.org/x b")
    assert out == "a `o.org` b"


@pytest.mark.asyncio
async def test_vet_answer_links_unlinks_only_what_oc8_never_issued(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await remember_links(db, tenant_id=tenant, urls=[GOOD])
        out = await vet_answer_links(db, tenant_id=tenant, text=f"[ok]({GOOD}) and [no]({BAD})")
    assert out == f"[ok]({GOOD}) and no"
