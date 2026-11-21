from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _evict() -> None:
    """Drop every cached `connector`/`mcp_bridge` module from sys.modules.

    Both names are generic -- after the package restructure every plugin ships
    a package called one of them (design §5.2) -- and sys.modules is keyed by
    NAME, not by path. Called SYMMETRICALLY on fixture setup AND teardown:
    before, so a sibling plugin's cached copy cannot answer our import; after,
    so nothing generic is left cached for anyone else. The teardown half is
    the load-bearing one -- `loader.import_entry_point` (Task 6's collision
    fix) only evicts modules IT ITSELF introduced, so a name left cached here
    makes a later `find_plugin`/`load_plugin` for gdrive_source or
    microsoft365 silently hand back THIS plugin's code. Verified live.
    """
    for _stale in [
        n
        for n in sys.modules
        if n in {"connector", "mcp_bridge", "events"}
        or n.startswith(("connector.", "mcp_bridge.", "events."))
    ]:
        del sys.modules[_stale]


@pytest.fixture(autouse=True)
def _plugin_path() -> Iterator[None]:
    """Make THIS plugin's packages the ones that resolve, for each test.

    Function-scoped and autouse, per Task 7's convention: every plugin import
    in this file sits INSIDE a test body and resolves at execution time, long
    after any collection-time module-top prelude would have run.
    """
    _evict()
    sys.path.insert(0, str(PLUGIN_ROOT))
    yield
    sys.path.remove(str(PLUGIN_ROOT))
    _evict()


pytestmark = pytest.mark.asyncio


async def test_gmail_search_calls_users_messages_list_for_the_resolved_mailbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_bridge import google_api
    from mcp_bridge.tools import gmail as tools_gmail

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"messages": [{"id": "m1"}]})

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "resolve_mailbox_token", lambda mailbox: "tok")

    result = await tools_gmail.gmail_search({"query": "is:unread", "mailbox": "a@company.com"})
    assert captured["path"] == "/gmail/v1/users/a@company.com/messages"
    assert captured["params"]["q"] == "is:unread"
    assert result == {"messages": [{"id": "m1"}]}


async def test_gmail_send_builds_the_mime_message_from_structured_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model cannot reliably produce base64url-encoded RFC 2822 bytes itself
    -- this tool builds it from structured to/subject/body and asserts the
    decoded `raw` round-trips to a real message with the expected headers,
    the mock-discipline test the Microsoft plugin's own mail_send never
    needed (it never took a raw-message input in the first place)."""
    import base64
    from email import message_from_bytes, policy

    from mcp_bridge import google_api
    from mcp_bridge.tools import gmail as tools_gmail

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "sent1"})

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "resolve_mailbox_token", lambda mailbox: "tok")

    result = await tools_gmail.gmail_send(
        {
            "mailbox": "a@company.com",
            "to": ["bob@customer.com"],
            "subject": "Hello",
            "body": "Hi Bob",
        }
    )
    assert captured["path"] == "/gmail/v1/users/a@company.com/messages/send"
    # `message_from_bytes` defaults to the legacy `compat32` policy, whose
    # `Message` has no `get_content()` -- only `EmailMessage` (the modern
    # `policy.default` class) does. The brief's literal snippet omitted this
    # and would AttributeError regardless of the implementation; passing
    # `policy=policy.default` here is the standard-library fix, not a
    # behavior change to anything under test.
    decoded = message_from_bytes(
        base64.urlsafe_b64decode(captured["body"]["raw"]), policy=policy.default
    )
    assert decoded["To"] == "bob@customer.com"
    assert decoded["Subject"] == "Hello"
    assert decoded.get_content().strip() == "Hi Bob"
    assert result == {"id": "sent1"}


async def test_gmail_reply_threads_with_in_reply_to_and_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gmail requires In-Reply-To/References set to the original's Message-ID
    for the reply to render threaded in most mail clients -- threadId alone
    is not enough."""
    import base64
    from email import message_from_bytes

    from mcp_bridge import google_api
    from mcp_bridge.tools import gmail as tools_gmail

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "payload": {
                        "headers": [
                            {"name": "Message-ID", "value": "<original@company.com>"},
                            {"name": "Subject", "value": "Original subject"},
                        ]
                    }
                },
            )
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "sent2"})

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "resolve_mailbox_token", lambda mailbox: "tok")

    await tools_gmail.gmail_reply(
        {
            "mailbox": "a@company.com",
            "threadId": "t1",
            "to": ["bob@customer.com"],
            "body": "Replying",
        }
    )
    decoded = message_from_bytes(base64.urlsafe_b64decode(captured["body"]["raw"]))
    assert decoded["In-Reply-To"] == "<original@company.com>"
    assert decoded["References"] == "<original@company.com>"
    assert decoded["Subject"] == "Re: Original subject"
    # No threadId in the original's answer: the id given is kept.
    assert captured["body"]["threadId"] == "t1"


async def test_gmail_reply_sends_into_the_original_messages_own_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message id is not its thread id (a reply inside a thread): the reply
    goes into the thread Gmail reports for the original, never the id given."""
    import json

    from mcp_bridge import google_api
    from mcp_bridge.tools import gmail as tools_gmail

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            seen["path"] = request.url.path
            return httpx.Response(
                200,
                json={
                    "id": "18c00msg",
                    "threadId": "18bffthread",
                    "payload": {"headers": [{"name": "Message-ID", "value": "<o@x.com>"}]},
                },
            )
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "sent"})

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "resolve_mailbox_token", lambda mailbox: "tok")

    for args in ({"messageId": "18c00msg"}, {"threadId": "18c00msg"}):
        await tools_gmail.gmail_reply(
            {"mailbox": "a@company.com", "to": ["bob@customer.com"], "body": "Hi", **args}
        )
        assert seen["path"].endswith("/messages/18c00msg")
        assert seen["body"]["threadId"] == "18bffthread"
    with pytest.raises(RuntimeError, match="messageId"):
        await tools_gmail.gmail_reply({"to": ["bob@customer.com"], "body": "Hi"})
    [schema] = [t.input_schema for t in tools_gmail.TOOLS if t.name == "gmail_reply"]
    assert "messageId" in schema["properties"] and "threadId" not in schema["required"]


async def test_gmail_reply_threads_when_the_original_header_is_mixed_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whole-branch review I1. RFC 5322 header names are case-insensitive, and
    the Gmail API returns each header's name AS IT APPEARED in the original
    message -- `Message-Id` is emitted by a great many MTAs and mail clients.
    An exact-case `.get("Message-ID")` therefore returns "" on real mail from
    real senders, `extra_headers` becomes `{}`, and the reply goes out with no
    In-Reply-To/References at all: no error, no warning, it just renders
    unthreaded, which is precisely the failure this tool exists to prevent.
    The sibling test above mocks `Message-ID` and so encoded the belief rather
    than testing it."""
    import base64
    from email import message_from_bytes

    from mcp_bridge import google_api
    from mcp_bridge.tools import gmail as tools_gmail

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "payload": {
                        "headers": [
                            # Mixed case, exactly as a real MTA emits it.
                            {"name": "Message-Id", "value": "<original@company.com>"},
                            {"name": "subject", "value": "Original subject"},
                        ]
                    }
                },
            )
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "sent3"})

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "resolve_mailbox_token", lambda mailbox: "tok")

    await tools_gmail.gmail_reply(
        {
            "mailbox": "a@company.com",
            "threadId": "t1",
            "to": ["bob@customer.com"],
            "body": "Replying",
        }
    )
    decoded = message_from_bytes(base64.urlsafe_b64decode(captured["body"]["raw"]))
    assert decoded["In-Reply-To"] == "<original@company.com>"
    assert decoded["References"] == "<original@company.com>"
    assert decoded["Subject"] == "Re: Original subject"


# A structurally real `users.messages.get?format=full` response: the body of a
# normal Gmail message with both an HTML and a plain-text alternative, wrapped
# in the `multipart/mixed` Gmail produces once an attachment is present. Checked
# against the Gmail API's MessagePart reference -- `parts` nest, and the
# `text/plain` part is NOT always at the top level.
def _b64(text: str) -> str:
    import base64

    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


_MULTIPART_MESSAGE = {
    "id": "m9",
    "threadId": "t9",
    "labelIds": ["INBOX", "UNREAD"],
    "snippet": "Hi Alex, about the invoice",
    "payload": {
        "mimeType": "multipart/mixed",
        "headers": [
            {"name": "From", "value": "bob@customer.com"},
            {"name": "Subject", "value": "Invoice question"},
        ],
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "body": {"size": 42, "data": _b64("Hi Alex,\n\nabout the invoice.\n")},
                    },
                    {
                        "mimeType": "text/html",
                        "body": {"size": 60, "data": _b64("<p>Hi Alex,</p>")},
                    },
                ],
            },
            {
                "mimeType": "application/pdf",
                "filename": "invoice.pdf",
                "body": {"attachmentId": "att1", "size": 1024},
            },
        ],
    },
}


async def test_gmail_get_returns_the_decoded_body_not_base64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whole-branch review I7. `format=full` returns the body base64url-ENCODED
    under `payload.body.data` / `payload.parts[].body.data`; returning that JSON
    verbatim hands the model an unreadable blob with only the ~200-char
    `snippet` in plain text. This is a regression against the sibling Microsoft
    plugin, whose `mail_get` returns readable body content."""
    from mcp_bridge import google_api
    from mcp_bridge.tools import gmail as tools_gmail

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_MULTIPART_MESSAGE)

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "resolve_mailbox_token", lambda mailbox: "tok")

    result = await tools_gmail.gmail_get({"mailbox": "a@company.com", "messageId": "m9"})
    assert result["body"] == "Hi Alex,\n\nabout the invoice.\n"
    assert result["bodyMimeType"] == "text/plain"
    # Headers come back casefolded, the same map gmail_reply threads from.
    assert result["headers"]["subject"] == "Invoice question"
    assert result["headers"]["from"] == "bob@customer.com"
    assert result["snippet"] == "Hi Alex, about the invoice"
    assert result["threadId"] == "t9"


async def test_gmail_get_falls_back_to_html_and_says_it_is_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message with no plain-text alternative is common (marketing mail, and
    anything sent from an HTML-only client). Returning the markup is better
    than returning nothing -- but returning it as if it were prose, with no way
    for the caller to tell, is its own wrong answer."""
    from mcp_bridge import google_api
    from mcp_bridge.tools import gmail as tools_gmail

    html_only = {
        "id": "m10",
        "threadId": "t10",
        "snippet": "Newsletter",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "Subject", "value": "Newsletter"}],
            "parts": [
                {"mimeType": "text/html", "body": {"data": _b64("<h1>Newsletter</h1>")}},
            ],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=html_only)

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "resolve_mailbox_token", lambda mailbox: "tok")

    result = await tools_gmail.gmail_get({"mailbox": "a@company.com", "messageId": "m10"})
    assert result["body"] == "<h1>Newsletter</h1>"
    assert result["bodyMimeType"] == "text/html"


async def test_gmail_get_decodes_a_simple_non_multipart_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_bridge import google_api
    from mcp_bridge.tools import gmail as tools_gmail

    simple = {
        "id": "m11",
        "threadId": "t11",
        "snippet": "Short one",
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": "Subject", "value": "Short one"}],
            "body": {"data": _b64("Just a line.\n")},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=simple)

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "resolve_mailbox_token", lambda mailbox: "tok")

    result = await tools_gmail.gmail_get({"mailbox": "a@company.com", "messageId": "m11"})
    assert result["body"] == "Just a line.\n"
    assert result["bodyMimeType"] == "text/plain"
