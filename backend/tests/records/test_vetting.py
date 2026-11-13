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


def test_links_inside_code_are_left_alone() -> None:
    text = f"`{BAD}`\n```\n{BAD}\n```"
    assert vet_links(text, connected_hosts=HOSTS, issued=set()) == text


@pytest.mark.asyncio
async def test_vet_answer_links_unlinks_only_what_oc8_never_issued(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await remember_links(db, tenant_id=tenant, urls=[GOOD])
        out = await vet_answer_links(db, tenant_id=tenant, text=f"[ok]({GOOD}) and [no]({BAD})")
    assert out == f"[ok]({GOOD}) and no"
