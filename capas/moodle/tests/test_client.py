from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _evict() -> None:
    """Drop every cached `connector`/`mcp_bridge` module: both names are shared
    by every capa, and sys.modules is keyed by name, not path."""
    for _stale in [
        n
        for n in sys.modules
        if n in {"connector", "mcp_bridge"} or n.startswith(("connector.", "mcp_bridge."))
    ]:
        del sys.modules[_stale]


_evict()
sys.path.insert(0, str(PLUGIN_ROOT))

from mcp_bridge import client  # noqa: E402

sys.path.remove(str(PLUGIN_ROOT))
_evict()

TOKEN = "s3cr3t-moodle-token"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(client.URL_ENV, "https://lms.example.org/")
    monkeypatch.setenv(client.TOKEN_ENV, TOKEN)


def _serve(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        result: httpx.Response = handler(request)
        return result

    monkeypatch.setattr(client, "transport", httpx.MockTransport(wrapped))
    return seen


def test_encode_flattens_nested_lists_and_dicts_the_way_moodle_reads_them() -> None:
    assert client.encode(
        {
            "courseids": [5, 7],
            "users": [{"username": "ana", "auth": "manual"}],
            "plugindata": {"assignfeedbackcomments_editor": {"text": "Gut", "format": 1}},
            "visible": True,
            "skip": None,
        }
    ) == {
        "courseids[0]": "5",
        "courseids[1]": "7",
        "users[0][username]": "ana",
        "users[0][auth]": "manual",
        "plugindata[assignfeedbackcomments_editor][text]": "Gut",
        "plugindata[assignfeedbackcomments_editor][format]": "1",
        "visible": "1",
    }


@pytest.mark.asyncio
async def test_call_posts_token_and_function_in_the_body_never_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _serve(monkeypatch, lambda r: httpx.Response(200, json={"sitename": "LMS"}))
    assert await client.call("core_webservice_get_site_info") == {"sitename": "LMS"}
    request = seen[0]
    assert str(request.url) == "https://lms.example.org/webservice/rest/server.php"
    body = request.content.decode()
    assert "wstoken=" + TOKEN in body
    assert "wsfunction=core_webservice_get_site_info" in body
    assert "moodlewsrestformat=json" in body
    assert TOKEN not in str(request.url)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (
            {
                "exception": "moodle_exception",
                "errorcode": "invalidtoken",
                "message": "x",
            },
            "token is invalid or expired",
        ),
        (
            {
                "exception": "webservice_access_exception",
                "errorcode": "accessexception",
                "message": "Access control exception",
            },
            "does not allow core_user_create_users",
        ),
        (
            {
                "exception": "required_capability_exception",
                "errorcode": "nopermissions",
                "message": "Sorry, but you do not currently have permissions to do that.",
            },
            "lacks permission for this: Sorry",
        ),
        (
            {
                "exception": "invalid_parameter_exception",
                "errorcode": "invalidparameter",
                "message": "Username already exists: ana",
            },
            "Moodle rejected the request (invalidparameter): Username already exists: ana",
        ),
    ],
)
@pytest.mark.asyncio
async def test_moodle_errors_arrive_as_http_200_and_become_readable_errors(
    monkeypatch: pytest.MonkeyPatch, answer: dict[str, str], expected: str
) -> None:
    _serve(monkeypatch, lambda r: httpx.Response(200, json=answer))
    with pytest.raises(client.MoodleError) as caught:
        await client.call("core_user_create_users", {"users": [{"username": "ana"}]})
    assert expected in str(caught.value)


@pytest.mark.asyncio
async def test_an_unreachable_site_names_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _serve(monkeypatch, refuse)
    with pytest.raises(client.MoodleError, match=r"https://lms\.example\.org is not reachable"):
        await client.call("core_webservice_get_site_info")


@pytest.mark.asyncio
async def test_a_timeout_is_reported_as_such(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    _serve(monkeypatch, slow)
    with pytest.raises(client.MoodleError, match="timed out after 20 s"):
        await client.call("core_webservice_get_site_info")


@pytest.mark.asyncio
async def test_a_non_200_answer_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, lambda r: httpx.Response(502, text="bad gateway"))
    with pytest.raises(client.MoodleError, match="HTTP 502"):
        await client.call("core_webservice_get_site_info")


@pytest.mark.asyncio
async def test_html_instead_of_json_points_at_the_url_and_rest_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, lambda r: httpx.Response(200, text="<html>login</html>"))
    with pytest.raises(client.MoodleError, match="REST protocol"):
        await client.call("core_webservice_get_site_info")


@pytest.mark.asyncio
async def test_the_token_is_redacted_from_every_error_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def leaky(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed sending wstoken={TOKEN}", request=request)

    _serve(monkeypatch, leaky)
    with pytest.raises(client.MoodleError) as caught:
        await client.call("core_webservice_get_site_info")
    assert TOKEN not in str(caught.value)
    assert "***" in str(caught.value)

    _serve(
        monkeypatch,
        lambda r: httpx.Response(
            200, json={"exception": "x", "errorcode": "y", "message": f"bad token {TOKEN}"}
        ),
    )
    with pytest.raises(client.MoodleError) as caught2:
        await client.call("core_webservice_get_site_info")
    assert TOKEN not in str(caught2.value)


def test_site_url_strips_the_trailing_slash() -> None:
    assert client.site_url() == "https://lms.example.org"


@pytest.mark.parametrize("url", ["http://lms.example.org", "ftp://lms.example.org", "lms"])
def test_site_url_refuses_plain_http_and_garbage(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv(client.URL_ENV, url)
    with pytest.raises(client.MoodleError):
        client.site_url()


@pytest.mark.parametrize(
    "url", ["http://localhost:8080", "http://host.docker.internal:8081", "http://127.0.0.1"]
)
def test_site_url_allows_plain_http_for_local_development(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv(client.URL_ENV, url)
    assert client.site_url() == url


@pytest.mark.asyncio
async def test_missing_configuration_says_what_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(client.TOKEN_ENV)
    with pytest.raises(client.MoodleError, match="MOODLE_TOKEN is not set"):
        await client.call("core_webservice_get_site_info")
