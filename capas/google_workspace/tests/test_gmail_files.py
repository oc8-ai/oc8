"""Gmail attachments as run files: gmail_get lists them, gmail_get_attachment
returns one as MCP binary content (core keeps it as a file of the run), and
gmail_send/gmail_reply/gmail_create_draft attach run files, which core hands
over resolved (oc8.agent.run_files)."""

from __future__ import annotations

import base64
import json
import sys
from collections.abc import Iterator
from email import message_from_bytes, policy
from pathlib import Path
from typing import Any

import httpx
import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PDF = b"%PDF-1.4 \x00\x01 statement" * 20

pytestmark = pytest.mark.asyncio


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


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


ATTACHMENT_ID = "ANGjdJ_" + "x" * 300 + "-9"
MESSAGE = {
    "id": "m9",
    "threadId": "t9",
    "snippet": "",
    "payload": {
        "mimeType": "multipart/mixed",
        "headers": [{"name": "Subject", "value": "Statement"}],
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64url(b"See attached.")}},
            {
                "mimeType": "multipart/mixed",
                "parts": [
                    {
                        "partId": "1.0",
                        "mimeType": "application/pdf",
                        "filename": "statement.pdf",
                        "body": {"attachmentId": ATTACHMENT_ID, "size": len(PDF)},
                    }
                ],
            },
        ],
    },
}


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    from mcp_bridge import google_api

    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.method == "GET" and "/attachments/" in request.url.path:
            return httpx.Response(200, json={"size": len(PDF), "data": _b64url(PDF)})
        if request.method == "GET":
            return httpx.Response(200, json=MESSAGE)
        return httpx.Response(200, json={"id": "sent1", "threadId": "t9"})

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "resolve_mailbox_token", lambda mailbox: "tok")
    return sent


def _run_file(name: str, mime: str, data: bytes) -> dict[str, Any]:
    return {
        "file_id": "0190f0aa-0000-7000-8000-000000000001",
        "name": name,
        "mime_type": mime,
        "size": len(data),
        "content_base64": base64.b64encode(data).decode(),
    }


async def test_gmail_get_lists_the_attachments(api: list[httpx.Request]) -> None:
    from mcp_bridge.tools import gmail

    result = await gmail.gmail_get({"messageId": "m9"})
    assert result["attachments"] == [
        {
            "partId": "1.0",
            "attachmentId": ATTACHMENT_ID,
            "filename": "statement.pdf",
            "mimeType": "application/pdf",
            "size": len(PDF),
        }
    ]


@pytest.mark.parametrize("key", ["partId", "attachmentId"])
async def test_gmail_get_attachment_returns_the_file(key: str, api: list[httpx.Request]) -> None:
    """By partId, which stays the same across reads; Gmail hands out a new
    attachmentId on every read of the message, so that one only works while
    it still matches."""
    from mcp_bridge.files import FileContent
    from mcp_bridge.tools import gmail

    ref = {"partId": "1.0", "attachmentId": ATTACHMENT_ID}[key]
    result = await gmail.gmail_get_attachment({"messageId": "m9", key: ref})
    assert isinstance(result, FileContent)
    assert (result.name, result.mime_type, result.data) == (
        "statement.pdf",
        "application/pdf",
        PDF,
    )
    assert result.details["size"] == len(PDF)
    assert api[-1].url.raw_path.decode() == (
        f"/gmail/v1/users/me/messages/m9/attachments/{ATTACHMENT_ID}"
    )


@pytest.mark.parametrize(
    "args",
    [
        {"messageId": "../m9", "attachmentId": ATTACHMENT_ID},
        {"messageId": "m9", "attachmentId": "a/../b"},
        {"messageId": "m9", "attachmentId": "a?alt=media"},
        {"messageId": "m9", "partId": "9"},
        {"messageId": "m9"},
    ],
)
async def test_gmail_get_attachment_refuses_what_is_not_there(
    args: dict[str, str], api: list[httpx.Request]
) -> None:
    from mcp_bridge.tools import gmail

    with pytest.raises(RuntimeError, match=r"Gmail id|no attachment|partId"):
        await gmail.gmail_get_attachment(args)
    assert not any("/attachments/" in r.url.path for r in api)


async def test_an_attachment_id_alone_is_fetched_as_given(api: list[httpx.Request]) -> None:
    """Gmail hands out a new attachmentId on every read, so one from an
    earlier gmail_get no longer matches the message read now: it is fetched
    directly, once it is a well-formed id, and the part with its size names it."""
    from mcp_bridge.tools import gmail

    earlier = "ANGjdJ_" + "y" * 300
    result = await gmail.gmail_get_attachment({"messageId": "m9", "attachmentId": earlier})
    assert (result.name, result.mime_type, result.data) == (
        "statement.pdf",
        "application/pdf",
        PDF,
    )
    assert api[-1].url.raw_path.decode() == f"/gmail/v1/users/me/messages/m9/attachments/{earlier}"


INLINE = {
    "partId": "2",
    "mimeType": "text/calendar",
    "filename": "invite.ics",
    "body": {"data": _b64url(b"BEGIN:VCALENDAR"), "size": 15},
}


async def test_a_small_attachment_carried_inline_is_listed_and_returned(
    api: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gmail puts a small part's content in `body.data` and gives it no
    attachmentId; it is an attachment all the same."""
    import copy

    from mcp_bridge.tools import gmail

    message = copy.deepcopy(MESSAGE)
    message["payload"]["parts"].append(INLINE)
    monkeypatch.setitem(globals(), "MESSAGE", message)

    listed = (await gmail.gmail_get({"messageId": "m9"}))["attachments"]
    assert {"partId": "2", "attachmentId": "", "filename": "invite.ics",
            "mimeType": "text/calendar", "size": 15} in listed
    result = await gmail.gmail_get_attachment({"messageId": "m9", "partId": "2"})
    assert (result.name, result.mime_type, result.data) == (
        "invite.ics",
        "text/calendar",
        b"BEGIN:VCALENDAR",
    )
    assert not any("/attachments/" in r.url.path for r in api)


@pytest.mark.parametrize("tool", ["gmail_send", "gmail_reply", "gmail_create_draft"])
async def test_sending_tools_attach_run_files(tool: str, api: list[httpx.Request]) -> None:
    from mcp_bridge.tools import gmail

    await gmail.CALL_HANDLERS[tool](
        {
            "messageId": "m9",
            "to": ["bob@customer.com"],
            "subject": "Statement",
            "body": "Attached.",
            "attachments": [
                {"file": _run_file("statement.pdf", "application/pdf", PDF)},
                {"file": _run_file("x.pdf", "application/pdf", PDF), "name": "renamed.pdf"},
            ],
        }
    )
    body = json.loads(api[-1].content)
    raw = (body.get("message") or body)["raw"]
    msg = message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)
    files = {p.get_filename(): p for p in msg.iter_attachments()}
    assert set(files) == {"statement.pdf", "renamed.pdf"}
    assert files["statement.pdf"].get_content_type() == "application/pdf"
    assert files["statement.pdf"].get_payload(decode=True) == PDF


async def test_an_unresolved_file_reference_is_refused(api: list[httpx.Request]) -> None:
    from mcp_bridge.tools import gmail

    with pytest.raises(RuntimeError, match="file id"):
        await gmail.gmail_send(
            {
                "to": ["bob@customer.com"],
                "subject": "s",
                "body": "b",
                "attachments": [{"file": "0190f0aa-0000-7000-8000-000000000001"}],
            }
        )
    assert api == []


def test_sending_tools_mark_their_attachments_as_run_files() -> None:
    from mcp_bridge.tools import gmail

    by_name = {t.name: t for t in gmail.TOOLS}
    for name in ("gmail_send", "gmail_reply", "gmail_create_draft"):
        items = by_name[name].input_schema["properties"]["attachments"]["items"]
        assert items["properties"]["file"]["x-oc8-file"] is True, name
    assert "gmail_get_attachment" in gmail.CALL_HANDLERS


async def test_the_bridge_sends_a_file_as_binary_content(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp.types as types
    from mcp_bridge import __main__ as bridge
    from mcp_bridge.files import FileContent

    async def handler(args: dict[str, object]) -> object:
        return FileContent("Q3 report.pdf", "application/pdf", PDF, {"size": len(PDF)})

    monkeypatch.setitem(bridge.ALL_HANDLERS, "gmail_get_attachment", handler)
    result = await bridge._on_call_tool(
        None, types.CallToolRequestParams(name="gmail_get_attachment", arguments={})
    )
    details, blob = result.content
    assert json.loads(details.text) == {"size": len(PDF)}
    assert isinstance(blob, types.EmbeddedResource)
    assert str(blob.resource.uri) == "attachment:///Q3%20report.pdf"
    assert base64.b64decode(blob.resource.blob) == PDF
