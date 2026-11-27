"""A Gmail message or thread id goes into a URL path, so it is checked to be
one (`[A-Za-z0-9_-]`, at most 64) and percent-quoted on the way in. A model
passing `../drafts` or `m1?alt=media` must not reach another endpoint of the
mailbox; the call fails before any request leaves."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _evict() -> None:
    for stale in [
        n
        for n in sys.modules
        if n in {"connector", "mcp_bridge", "events"}
        or n.startswith(("connector.", "mcp_bridge.", "events."))
    ]:
        del sys.modules[stale]


@pytest.fixture(autouse=True)
def _plugin_path() -> Iterator[None]:
    _evict()
    sys.path.insert(0, str(PLUGIN_ROOT))
    yield
    sys.path.remove(str(PLUGIN_ROOT))
    _evict()


@pytest.fixture
def requests(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    from mcp_bridge import google_api

    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            200, json={"id": "m1", "threadId": "t1", "payload": {"headers": []}}
        )

    monkeypatch.setattr(
        google_api,
        "get_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(google_api, "resolve_mailbox_token", lambda mailbox: "tok")
    return sent


BAD_IDS = [
    "../drafts",
    "m1/../../labels",
    "m1?alt=media",
    "m1#x",
    "m 1",
    "%2e%2e",
    "m1\r\nX: y",
    "a" * 65,
    "",
]

CALLS: list[tuple[str, dict[str, Any], str]] = [
    ("gmail_get", {}, "messageId"),
    ("gmail_reply", {"to": ["b@c.com"], "body": "x"}, "messageId"),
    ("gmail_reply", {"to": ["b@c.com"], "body": "x"}, "threadId"),
    ("gmail_modify_labels", {"addLabelIds": ["STARRED"]}, "messageId"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "args", "key"), CALLS)
@pytest.mark.parametrize("bad", BAD_IDS)
async def test_a_bad_id_is_refused_before_any_request(
    tool: str, args: dict[str, Any], key: str, bad: str, requests: list[httpx.Request]
) -> None:
    from mcp_bridge.tools import gmail

    with pytest.raises(Exception, match="Gmail id"):
        await gmail.CALL_HANDLERS[tool]({**args, key: bad})
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "args", "key"), CALLS)
async def test_a_good_id_reaches_its_own_path(
    tool: str, args: dict[str, Any], key: str, requests: list[httpx.Request]
) -> None:
    from mcp_bridge.tools import gmail

    await gmail.CALL_HANDLERS[tool]({**args, key: "18c2f0A_b-9"})
    assert (
        requests[0]
        .url.raw_path.decode()
        .startswith("/gmail/v1/users/me/messages/18c2f0A_b-9")
    )


def test_quoting_leaves_nothing_that_could_split_a_path() -> None:
    """The check is what keeps a path whole; quoting with safe="" is the
    second line, should a looser check ever let a character through."""
    from mcp_bridge.tools import gmail

    assert gmail._path_id("18c2f0A_b-9", "messageId") == "18c2f0A_b-9"
    with pytest.raises(RuntimeError, match="Gmail id"):
        gmail._path_id("a/b", "messageId")
