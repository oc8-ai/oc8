"""The search providers against a fake search server on loopback: each maps
its answer to `SearchHit`s, at most 8 of them, with a 10 s timeout, one
retry and any non-200 as `WebSearchError`. SearXNG's address passes the same
outbound guard as `fetch_url`, except the operator's own instance."""

from __future__ import annotations

import pytest

from oc8.websearch import providers
from oc8.websearch.providers import (
    MAX_RESULTS,
    TIMEOUT_S,
    ProviderConfig,
    SearchHit,
    WebSearchError,
    run_search,
)
from tests.websearch.conftest import FakeSearch, rows

pytestmark = pytest.mark.asyncio


def _searxng(fake: FakeSearch) -> ProviderConfig:
    """The operator's own instance: its host may be internal."""
    return ProviderConfig(provider="searxng", base_url=fake.base, internal_host="127.0.0.1")


async def test_searxng_maps_results_to_hits(fake: FakeSearch) -> None:
    fake.answers = [(200, {"results": rows(3, "content")})]
    hits = await run_search(_searxng(fake), "oc8 release", 5)
    assert hits == [
        SearchHit(title=f"Hit {i}", url=f"https://example.org/{i}", snippet=f"About {i}")
        for i in (1, 2, 3)
    ]
    (seen,) = fake.seen
    assert seen["path"] == "/search"
    assert seen["query"] == {"q": ["oc8 release"], "format": ["json"]}


async def test_brave_maps_results_and_sends_the_key_as_a_header(
    fake: FakeSearch, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(providers, "BRAVE_ENDPOINT", f"{fake.base}/res/v1/web/search")
    fake.answers = [(200, {"web": {"results": rows(2, "description")}})]
    hits = await run_search(ProviderConfig(provider="brave", api_key="brv-k"), "weather", 4)
    assert [h.snippet for h in hits] == ["About 1", "About 2"]
    (seen,) = fake.seen
    assert seen["headers"]["X-Subscription-Token"] == "brv-k"
    assert seen["query"] == {"q": ["weather"], "count": ["4"]}


async def test_tavily_maps_results_and_posts_the_query(
    fake: FakeSearch, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(providers, "TAVILY_ENDPOINT", f"{fake.base}/search")
    fake.answers = [(200, {"results": rows(2, "content")})]
    hits = await run_search(ProviderConfig(provider="tavily", api_key="tvly-k"), "news", 2)
    assert [h.title for h in hits] == ["Hit 1", "Hit 2"]
    (seen,) = fake.seen
    assert seen["method"] == "POST"
    assert seen["body"] == {"query": "news", "max_results": 2}
    assert seen["headers"]["Authorization"] == "Bearer tvly-k"


async def test_never_more_than_eight_hits(
    fake: FakeSearch, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(providers, "BRAVE_ENDPOINT", f"{fake.base}/x")
    fake.answers = [(200, {"web": {"results": rows(20, "description")}})]
    hits = await run_search(ProviderConfig(provider="brave", api_key="k"), "q", 50)
    assert len(hits) == MAX_RESULTS == 8
    # The provider is asked for no more either.
    assert fake.seen[0]["query"]["count"] == ["8"]


async def test_rows_without_a_web_address_are_dropped(fake: FakeSearch) -> None:
    fake.answers = [
        (
            200,
            {
                "results": [
                    {"title": "js", "url": "javascript:alert(1)", "content": "x"},
                    {"title": "none", "content": "x"},
                    {"title": "ok", "url": "https://ok.example/", "content": "fine"},
                ]
            },
        )
    ]
    hits = await run_search(_searxng(fake), "q", 5)
    assert [h.url for h in hits] == ["https://ok.example/"]


async def test_a_client_error_is_a_search_error_without_a_retry(
    fake: FakeSearch, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(providers, "BRAVE_ENDPOINT", f"{fake.base}/x")
    fake.answers = [(401, {"error": "bad key"})]
    with pytest.raises(WebSearchError) as caught:
        await run_search(ProviderConfig(provider="brave", api_key="secret-key"), "q", 3)
    assert "401" in str(caught.value)
    assert "secret-key" not in str(caught.value)
    assert len(fake.seen) == 1


async def test_a_server_error_is_retried_once(fake: FakeSearch) -> None:
    fake.answers = [(503, {}), (200, {"results": rows(1, "content")})]
    hits = await run_search(_searxng(fake), "q", 3)
    assert len(hits) == 1
    assert len(fake.seen) == 2


async def test_a_server_error_twice_is_a_search_error(fake: FakeSearch) -> None:
    fake.answers = [(500, {})]
    with pytest.raises(WebSearchError, match="500"):
        await run_search(_searxng(fake), "q", 3)
    assert len(fake.seen) == 2


async def test_a_slow_provider_times_out_after_one_retry(
    fake: FakeSearch, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert TIMEOUT_S == 10.0
    monkeypatch.setattr(providers, "TIMEOUT_S", 0.2)
    fake.delay_s = 1.0
    fake.answers = [(200, {"results": []})]
    with pytest.raises(WebSearchError, match="did not answer"):
        await run_search(_searxng(fake), "q", 3)
    assert len(fake.seen) == 2


async def test_a_redirect_is_not_followed(fake: FakeSearch, second_fake: FakeSearch) -> None:
    """A provider answering with a redirect never sends the request (or its
    key) on: the target is not reached and the search fails."""
    fake.answers = [(302, {})]
    fake.location = f"{second_fake.base}/x"
    with pytest.raises(WebSearchError, match="302"):
        await run_search(_searxng(fake), "q", 3)
    assert len(fake.seen) == 1
    assert second_fake.seen == []


async def test_a_brave_redirect_does_not_carry_the_key_on(
    fake: FakeSearch, second_fake: FakeSearch, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(providers, "BRAVE_ENDPOINT", f"{fake.base}/x")
    fake.answers = [(307, {})]
    fake.location = f"{second_fake.base}/steal"
    with pytest.raises(WebSearchError, match="307"):
        await run_search(ProviderConfig(provider="brave", api_key="brv-k"), "q", 3)
    assert second_fake.seen == []


async def test_a_body_that_trickles_in_still_times_out(
    fake: FakeSearch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The timeout bounds the whole attempt, not each read: a server that
    sends one byte at a time cannot hold the turn for minutes."""
    monkeypatch.setattr(providers, "TIMEOUT_S", 0.3)
    fake.answers = [(200, {"results": rows(3, "content")})]
    fake.trickle_s = 0.02
    with pytest.raises(WebSearchError, match="did not answer"):
        await run_search(_searxng(fake), "q", 3)


@pytest.mark.parametrize(
    "base_url",
    ["http://[::1", "http://127.0.0.1:99999", "http://127.0.0.1:port", "http://exa mple.org"],
)
async def test_an_unusable_address_is_a_search_error_not_a_crash(base_url: str) -> None:
    config = ProviderConfig(provider="searxng", base_url=base_url, internal_host="127.0.0.1")
    with pytest.raises(WebSearchError) as caught:
        await run_search(config, "q", 3)
    # Only the class of what went wrong, never the address or a traceback.
    assert "99999" not in str(caught.value) and "[::1" not in str(caught.value)


async def test_hit_addresses_are_cleaned_and_bounded(fake: FakeSearch) -> None:
    fake.answers = [
        (
            200,
            {
                "results": [
                    {"title": "a", "url": "  https://a.example/x\n", "content": ""},
                    {"title": "b", "url": "https://b.example/\u0000evil", "content": ""},
                    {"title": "c", "url": "https://c.example/" + "x" * 2100, "content": ""},
                    {"title": "d", "url": "https://d.example/a b", "content": ""},
                ]
            },
        )
    ]
    hits = await run_search(_searxng(fake), "q", 8)
    assert [h.url for h in hits] == ["https://a.example/x", "https://b.example/evil"]


async def test_a_tenant_searxng_on_an_internal_address_is_refused(fake: FakeSearch) -> None:
    fake.answers = [(200, {"results": rows(1, "content")})]
    tenant = ProviderConfig(provider="searxng", base_url=fake.base)
    with pytest.raises(WebSearchError, match="not reachable"):
        await run_search(tenant, "q", 3)
    assert fake.seen == []


async def test_only_the_operators_own_host_is_exempt(fake: FakeSearch) -> None:
    fake.answers = [(200, {"results": rows(1, "content")})]
    other = ProviderConfig(provider="searxng", base_url=fake.base, internal_host="searxng.internal")
    with pytest.raises(WebSearchError, match="not reachable"):
        await run_search(other, "q", 3)
    assert fake.seen == []


async def test_a_tenant_searxng_the_operator_allowed_is_reached(
    fake: FakeSearch, allow_loopback_outbound: None
) -> None:
    fake.answers = [(200, {"results": rows(2, "content")})]
    hits = await run_search(ProviderConfig(provider="searxng", base_url=fake.base), "q", 3)
    assert len(hits) == 2


async def test_the_config_never_prints_its_key() -> None:
    assert "k-123" not in repr(ProviderConfig(provider="brave", api_key="k-123"))
