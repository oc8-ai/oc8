"""Outlook attachments as run files: mail_list_attachments names them,
mail_get_attachment returns one as MCP binary content (core keeps it as a
file of the run), and mail_send/mail_reply/mail_create_draft attach run
files, which core hands over resolved (oc8.agent.run_files)."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any

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


_evict()
sys.path.insert(0, str(PLUGIN_ROOT))

from mcp_bridge import __main__ as bridge  # noqa: E402
from mcp_bridge import graph  # noqa: E402
from mcp_bridge._user import DEFAULT_USER_ENV  # noqa: E402
from mcp_bridge.files import FileContent  # noqa: E402
from mcp_bridge.tools.mail import CALL_HANDLERS, TOOLS  # noqa: E402

sys.path.remove(str(PLUGIN_ROOT))
_evict()

pytestmark = pytest.mark.asyncio
PDF = b"%PDF-1.4 \x00\x01 offer" * 20


def _run_file(name: str, mime: str, data: bytes) -> dict[str, Any]:
    return {
        "file_id": "0190f0aa-0000-7000-8000-000000000001",
        "name": name,
        "mime_type": mime,
        "size": len(data),
        "content_base64": base64.b64encode(data).decode(),
    }


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, Any]]:
    seen: list[tuple[str, str, Any]] = []

    async def fake_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        seen.append(("GET", path, params))
        if path.endswith("/attachments"):
            return {
                "value": [
                    {
                        "id": "AAMk=1",
                        "name": "offer.pdf",
                        "contentType": "application/pdf",
                        "size": len(PDF),
                        "isInline": False,
                    }
                ]
            }
        return {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "id": "AAMk=1",
            "name": "offer.pdf",
            "contentType": "application/pdf",
            "size": len(PDF),
            "contentBytes": base64.b64encode(PDF).decode(),
        }

    async def fake_post(path: str, json: dict[str, Any]) -> dict[str, Any]:
        seen.append(("POST", path, json))
        return {}

    monkeypatch.setattr(graph, "get", fake_get)
    monkeypatch.setattr(graph, "post", fake_post)
    monkeypatch.setenv(DEFAULT_USER_ENV, "info@contoso.com")
    return seen


async def test_mail_list_attachments_names_them(calls: list[tuple[str, str, Any]]) -> None:
    result = await CALL_HANDLERS["mail_list_attachments"]({"messageId": "AAMk/x="})
    assert result == [
        {
            "id": "AAMk=1",
            "name": "offer.pdf",
            "contentType": "application/pdf",
            "size": len(PDF),
            "isInline": False,
        }
    ]
    # The model's id is one path segment, whatever it contains.
    assert calls[0][1] == "/users/info@contoso.com/messages/AAMk%2Fx%3D/attachments"


async def test_mail_get_attachment_returns_the_file(calls: list[tuple[str, str, Any]]) -> None:
    result = await CALL_HANDLERS["mail_get_attachment"](
        {"messageId": "AAMk", "attachmentId": "AAMk=1"}
    )
    assert isinstance(result, FileContent)
    assert (result.name, result.mime_type, result.data) == ("offer.pdf", "application/pdf", PDF)
    assert "contentBytes" not in json.dumps(result.details)
    assert calls[0][1] == "/users/info@contoso.com/messages/AAMk/attachments/AAMk%3D1"


async def test_an_attached_item_is_not_a_file(
    calls: list[tuple[str, str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def item(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"@odata.type": "#microsoft.graph.itemAttachment", "name": "Meeting"}

    monkeypatch.setattr(graph, "get", item)
    with pytest.raises(RuntimeError, match="not a file"):
        await CALL_HANDLERS["mail_get_attachment"]({"messageId": "m", "attachmentId": "a"})


@pytest.mark.parametrize("tool", ["mail_send", "mail_reply", "mail_create_draft"])
async def test_sending_tools_attach_run_files(tool: str, calls: list[tuple[str, str, Any]]) -> None:
    await CALL_HANDLERS[tool](
        {
            "messageId": "AAMk",
            "to": ["a@example.com"],
            "subject": "Offer",
            "body": "Attached.",
            "attachments": [
                {"file": _run_file("offer.pdf", "application/pdf", PDF)},
                {"file": _run_file("x.pdf", "application/pdf", PDF), "name": "renamed.pdf"},
            ],
        }
    )
    _, _, body = calls[-1]
    message = body["message"] if "message" in body else body
    assert message["attachments"] == [
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": name,
            "contentType": "application/pdf",
            "contentBytes": base64.b64encode(PDF).decode(),
        }
        for name in ("offer.pdf", "renamed.pdf")
    ]


@pytest.mark.parametrize("tool", ["mail_send", "mail_reply", "mail_create_draft"])
async def test_attachments_over_graphs_inline_limit_are_refused(
    tool: str, calls: list[tuple[str, str, Any]]
) -> None:
    # Under 3 MB as bytes, but its base64 (what the request carries) is over
    # the 2.9 MB that keeps a request under Graph's 4 MB.
    big = b"x" * (2 * 1024 * 1024 + 300 * 1024)
    with pytest.raises(RuntimeError, match=r"2\.9 MB"):
        await CALL_HANDLERS[tool](
            {
                "messageId": "AAMk",
                "to": ["a@example.com"],
                "subject": "s",
                "body": "b",
                "attachments": [{"file": _run_file("big.bin", "application/pdf", big)}],
            }
        )
    assert calls == []


async def test_attachments_under_graphs_inline_limit_go_together(
    calls: list[tuple[str, str, Any]],
) -> None:
    half = b"x" * (1024 * 1024)
    await CALL_HANDLERS["mail_send"](
        {
            "to": ["a@example.com"],
            "subject": "s",
            "body": "b",
            "attachments": [
                {"file": _run_file("a.bin", "application/pdf", half)},
                {"file": _run_file("b.bin", "application/pdf", half)},
            ],
        }
    )
    assert len(calls[-1][2]["message"]["attachments"]) == 2


@pytest.mark.parametrize(
    ("tool", "extra"),
    [
        ("mail_get", {}),
        ("mail_reply", {"body": "b"}),
        ("mail_move", {"destinationFolderId": "inbox"}),
        ("mail_delete", {}),
        ("mail_mark_read", {}),
    ],
)
async def test_a_message_id_is_one_path_segment(
    tool: str,
    extra: dict[str, Any],
    calls: list[tuple[str, str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_delete(path: str) -> None:
        calls.append(("DELETE", path, None))

    async def fake_patch(path: str, json: dict[str, Any]) -> dict[str, Any]:
        calls.append(("PATCH", path, json))
        return {}

    monkeypatch.setattr(graph, "delete", fake_delete)
    monkeypatch.setattr(graph, "patch", fake_patch)
    await CALL_HANDLERS[tool]({"messageId": "AAMk/../x?y=1", **extra})
    path = calls[-1][1]
    assert "/messages/AAMk%2F..%2Fx%3Fy%3D1" in path, path


async def test_a_missing_message_id_is_refused(calls: list[tuple[str, str, Any]]) -> None:
    with pytest.raises(RuntimeError, match="messageId"):
        await CALL_HANDLERS["mail_reply"]({"body": "b"})
    assert calls == []


async def test_attachments_over_three_mb_are_refused(
    calls: list[tuple[str, str, Any]],
) -> None:
    big = b"x" * (3 * 1024 * 1024 + 1)
    with pytest.raises(RuntimeError, match=r"2\.9 MB"):
        await CALL_HANDLERS["mail_send"](
            {
                "to": ["a@example.com"],
                "subject": "s",
                "body": "b",
                "attachments": [{"file": _run_file("big.bin", "application/pdf", big)}],
            }
        )
    assert calls == []


async def test_an_unresolved_file_reference_is_refused(
    calls: list[tuple[str, str, Any]],
) -> None:
    with pytest.raises(RuntimeError, match="file id"):
        await CALL_HANDLERS["mail_send"](
            {
                "to": ["a@example.com"],
                "subject": "s",
                "body": "b",
                "attachments": [{"file": "0190f0aa-0000-7000-8000-000000000001"}],
            }
        )
    assert calls == []


def test_sending_tools_mark_their_attachments_as_run_files() -> None:
    by_name = {t.name: t for t in TOOLS}
    for name in ("mail_send", "mail_reply", "mail_create_draft"):
        items = by_name[name].input_schema["properties"]["attachments"]["items"]
        assert items["properties"]["file"]["x-oc8-file"] is True, name


async def test_the_bridge_sends_a_file_as_binary_content(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp.types as types

    async def handler(args: dict[str, object]) -> object:
        return FileContent("Q3 offer.pdf", "application/pdf", PDF, {"size": len(PDF)})

    monkeypatch.setitem(bridge.ALL_HANDLERS, "mail_get_attachment", handler)
    result = await bridge._on_call_tool(
        None, types.CallToolRequestParams(name="mail_get_attachment", arguments={})
    )
    details, blob = result.content
    assert json.loads(details.text) == {"size": len(PDF)}
    assert isinstance(blob, types.EmbeddedResource)
    assert str(blob.resource.uri) == "attachment:///Q3%20offer.pdf"
    assert base64.b64decode(blob.resource.blob) == PDF
