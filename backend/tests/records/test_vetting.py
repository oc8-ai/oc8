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


I42 = "https://h.example/odoo/42"
BYPASS_HOSTS = {"h.example"}


@pytest.mark.parametrize(
    "text",
    [
        "[t](https://o.org/x)(https://h.example/99)",  # glued after another host
        "[t](https://o.org/x)https://h.example/99",
        "https://h.example/1[a b](https://h.example/2)",  # overlapping spans
        "[t](https://h.example./odoo/99)",  # host respelled
        "[t](https://h%2Eexample/odoo/99)",
        "[t](HTTPS://H.EXAMPLE/odoo/99)",
        f"[a]({I42})[b](https://h.example/odoo/99)",
    ],
)
def test_glued_overlapping_and_respelled_urls_are_neutralised(text: str) -> None:
    out = vet_links(text, connected_hosts=BYPASS_HOSTS, issued={I42})
    for guessed in ("h.example/odoo/99", "h.example/99", "h.example/2", "h.example/1"):
        assert guessed not in out


def test_an_issued_link_glued_to_a_guessed_one_is_judged_as_one_run() -> None:
    # Safe direction: what follows the issued URL is not just punctuation.
    out = vet_links(
        f"[a]({I42})[b](https://h.example/odoo/99)", connected_hosts=BYPASS_HOSTS, issued={I42}
    )
    assert "h.example/odoo/99" not in out


@pytest.mark.parametrize("text", [f"[t]({I42}).", f"see {I42}.", f"({I42})", f"{I42},"])
def test_an_issued_url_followed_by_punctuation_is_kept(text: str) -> None:
    assert vet_links(text, connected_hosts=BYPASS_HOSTS, issued={I42}) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[t](https://h.example/odoo/42,/../99)", "t"),
        ("see https://h.example/odoo/42./../99 now", "see `h.example` now"),
        ("[t](https://h.example/odoo/42!/../99)", "t"),
        ("[t](https://h.example/odoo/42.html)", "t"),
        ("[t](https\\://h.example/odoo/99)", "`[t](https\\://h.example/odoo/99)`"),
        ("[t](https:\\/\\/h.example/odoo/99)", "`[t](https:\\/\\/h.example/odoo/99)`"),
        ("[t][r]\n\n[r]: https\\://h.example/99", "[t][r]\n\n[r]: `https\\://h.example/99`"),
        ("[t](https://h\u3002example/odoo/99)", "t"),
        ("[t](https://\uff48.example/odoo/99)", "t"),
        ("[t](https://h.example\\.)", "t"),
    ],
)
def test_ambiguous_or_respelled_destinations_are_neutralised(text: str, expected: str) -> None:
    assert vet_links(text, connected_hosts=BYPASS_HOSTS, issued={I42}) == expected


def test_backslashes_to_other_hosts_are_left_alone() -> None:
    text = "[t](https\\://o.org/x) and C:\\dir\\file"
    assert vet_links(text, connected_hosts=BYPASS_HOSTS, issued={I42}) == text


def test_backticks_inside_a_neutralised_run_cannot_close_the_code_span() -> None:
    out = vet_links("a`b https\\://h.example/x`c d", connected_hosts=BYPASS_HOSTS, issued=set())
    assert "``https\\://h.example/x`c``" in out


@pytest.mark.parametrize(
    "text",
    [
        "[t](https&#58;//h.example/odoo/99)",
        "[t](&#104;ttps://h.example/odoo/99)",
        "[t][r]\n\n[r]: https&#58;//h.example/99",
    ],
)
def test_entity_hidden_schemes_are_escaped(text: str) -> None:
    out = vet_links(text, connected_hosts=BYPASS_HOSTS, issued={I42})
    escaped = text.replace("&", "&amp;")
    run = next(r for r in escaped.split() if "h.example" in r)
    assert out == escaped.replace(run, f"`{run}`")


def test_a_truncated_issued_url_is_no_longer_issued() -> None:
    cut = f"[S42]({I42})"[:-2]  # ".../odoo/4"
    out = vet_links(cut, connected_hosts=BYPASS_HOSTS, issued={I42})
    assert "h.example/odoo/4" not in out


@pytest.mark.parametrize(
    "text",
    [
        "see https://h.example\\odoo\\99 now",
        "see https://h.example\\@o.org/../odoo/99 now",
    ],
)
def test_backslashes_that_a_browser_reads_as_slashes_are_neutralised(text: str) -> None:
    out = vet_links(text, connected_hosts=BYPASS_HOSTS, issued={I42})
    assert out == "see `h.example` now"


def test_an_other_host_with_a_backslash_path_is_left_alone() -> None:
    text = "see https://o.org\\path now"
    assert vet_links(text, connected_hosts=BYPASS_HOSTS, issued={I42}) == text


ISSUED_ROUND5 = {I42, PAREN}


@pytest.mark.parametrize(
    "text",
    [
        f"**[S42]({I42})**",
        f"*[S42]({I42})*",
        f"_[S42]({I42})_",
        f"~~[S42]({I42})~~",
        f"**{I42}**",
        f"[S42]({I42})**:** x",
        f"| [S42]({I42})|",
        f"[S42]({I42}),[S43]({I42})",
        f"**[P]({PAREN})**",
    ],
)
def test_an_issued_link_inside_markdown_emphasis_or_tables_is_kept(text: str) -> None:
    assert vet_links(text, connected_hosts=BYPASS_HOSTS, issued=ISSUED_ROUND5) == text


@pytest.mark.parametrize(
    "text",
    [
        f"[S\\_42]({I42})",
        f"[S42]({I42})\\*",
        f"[S42]({I42})&nbsp;",
        f"see {I42}\\ now",
    ],
)
def test_an_issued_link_next_to_an_escape_or_entity_is_kept(text: str) -> None:
    assert vet_links(text, connected_hosts=BYPASS_HOSTS, issued=ISSUED_ROUND5) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"**[S99]({G})**", "**S99**"),
        (f"**{G}**", "**`h.example`**"),
        (f"[S42]({I42})/../99", "S42/../99"),
        (f"[S42]({I42})\\..\\99", "S42\\..\\99"),
        (f"[S42]({I42}x)", "S42"),
        (f'[S42]({I42}"x)', "S42"),
        ("[S42](https&#58;//h.example/odoo/42)", "`[S42](https&#58;//h.example/odoo/42)`"),
    ],
)
def test_emphasis_and_escapes_do_not_excuse_a_changed_destination(text: str, expected: str) -> None:
    assert vet_links(text, connected_hosts=BYPASS_HOSTS, issued=ISSUED_ROUND5) == expected


def test_a_backslash_url_longer_run_keeps_only_its_host() -> None:
    out = vet_links(
        "see https://h.example\\odoo\\99. now", connected_hosts=BYPASS_HOSTS, issued={I42}
    )
    assert out == "see `h.example`. now"
