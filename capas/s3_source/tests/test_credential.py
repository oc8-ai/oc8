"""validate_s3 guards a custom endpoint and connects to the checked address."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _evict() -> None:
    for _stale in [
        n
        for n in sys.modules
        if n in ("credential", "connector") or n.startswith(("credential.", "connector."))
    ]:
        del sys.modules[_stale]


_evict()
sys.path.insert(0, str(PLUGIN_ROOT))

from credential import validate_s3  # noqa: E402

sys.path.remove(str(PLUGIN_ROOT))
_evict()

pytestmark = pytest.mark.asyncio

_VALUES = {"access_key": "AK", "secret_key": "SK", "endpoint": "minio.acme.test:9000"}


async def test_an_internal_endpoint_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("oc8.net.outbound.resolve_ips", lambda host: ["10.0.0.5"])
    with pytest.raises(ValueError, match="not reachable"):
        await validate_s3(_VALUES)


async def test_a_scheme_less_endpoint_is_checked_and_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from oc8.net.outbound import PinnedTransport
    from oc8.oauth import http as oauth_http

    answers = iter([["93.184.216.34"], ["10.0.0.5"]])
    monkeypatch.setattr("oc8.net.outbound.resolve_ips", lambda host: next(answers))
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="<ListBucketResult/>")

    oauth_http.set_transport_override(httpx.MockTransport(handler))
    try:
        await validate_s3(_VALUES)
    finally:
        oauth_http.set_transport_override(None)
    assert seen[0].url.host == "93.184.216.34"  # not the second, private, answer
    assert seen[0].headers["Host"] == "minio.acme.test:9000"
    assert PinnedTransport


_AWS = {"access_key": "AK", "secret_key": "SK", "region": "eu-central-1", "endpoint": ""}


@pytest.mark.parametrize(
    "region",
    ["x@10.0.0.5:8443/#", "eu-central-1.evil.test#", "EU-CENTRAL-1", "a/b", "x" * 33, "eu central"],
)
async def test_a_region_that_is_not_a_region_name_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, region: str
) -> None:
    from oc8.credentials.errors import CredentialCheckFailed
    from oc8.oauth import http as oauth_http

    monkeypatch.setattr("oc8.net.outbound.resolve_ips", lambda host: ["93.184.216.34"])
    seen: list[httpx.Request] = []
    oauth_http.set_transport_override(
        httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, text="<x/>"))
    )
    try:
        with pytest.raises(CredentialCheckFailed) as caught:
            await validate_s3({**_AWS, "region": region})
    finally:
        oauth_http.set_transport_override(None)
    assert caught.value.reason == "invalid"
    assert caught.value.detail is not None and region not in caught.value.detail
    assert seen == []


async def test_an_aws_check_is_guarded_and_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    from oc8.oauth import http as oauth_http

    asked: list[str] = []
    answers = iter([["52.219.170.1"], ["10.0.0.5"]])

    def resolve(host: str) -> list[str]:
        asked.append(host)
        return next(answers)

    monkeypatch.setattr("oc8.net.outbound.resolve_ips", resolve)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="<ListAllMyBucketsResult/>")

    oauth_http.set_transport_override(httpx.MockTransport(handler))
    try:
        await validate_s3(_AWS)
    finally:
        oauth_http.set_transport_override(None)
    assert asked == ["s3.eu-central-1.amazonaws.com"]
    assert seen[0].url.host == "52.219.170.1"
    assert seen[0].headers["Host"] == "s3.eu-central-1.amazonaws.com"


async def test_an_aws_host_that_resolves_internally_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from oc8.credentials.errors import CredentialCheckFailed

    monkeypatch.setattr("oc8.net.outbound.resolve_ips", lambda host: ["10.0.0.5"])
    with pytest.raises(CredentialCheckFailed) as caught:
        await validate_s3(_AWS)
    assert caught.value.reason == "unreachable"
