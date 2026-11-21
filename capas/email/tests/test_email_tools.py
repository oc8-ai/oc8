"""The mail tools against a real IMAP/POP3/SMTP server (GreenMail), called
the way the bridge calls them: with the login in the environment."""

from __future__ import annotations

import base64
import email
import email.policy
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PLUGIN_ROOT = str(Path(__file__).resolve().parents[1])


async def call(tools: ModuleType, name: str, **args: Any) -> Any:
    return await tools.CALL_HANDLERS[name](args)


def parsed(raw: bytes) -> email.message.EmailMessage:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert isinstance(msg, email.message.EmailMessage)
    return msg


# --- reading ----------------------------------------------------------------


async def test_search_and_get_return_stable_ids(
    tools: ModuleType, greenmail: Any, mail_env: Callable[..., dict[str, str]], make_mail: Any
) -> None:
    greenmail.append(make_mail(subject="Invoice 7", message_id="<a@x>"))
    greenmail.append(make_mail(subject="Lunch", sender="bob@example.com", message_id="<b@x>"))

    found = await call(tools, "mail_search", subject="Invoice")
    assert [m["subject"] for m in found] == ["Invoice 7"]
    hit = found[0]
    folder, uidvalidity, uid = hit["id"].rsplit(":", 2)
    assert folder == "INBOX" and int(uidvalidity) > 0 and int(uid) > 0
    assert hit["from"] == "Alice <alice@example.com>"
    assert hit["unread"] is True

    again = await call(tools, "mail_search", subject="Invoice")
    assert again[0]["id"] == hit["id"]

    by_sender = await call(tools, "mail_search", **{"from": "bob@example.com"})
    assert [m["subject"] for m in by_sender] == ["Lunch"]
    everything = await call(tools, "mail_search", limit=1)
    assert len(everything) == 1

    mail = await call(tools, "mail_get", id=hit["id"])
    assert mail["subject"] == "Invoice 7"
    assert mail["message_id"] == "<a@x>"
    assert "please have a look" in mail["text"]
    assert mail["attachments"] == []
    # Reading is not marking read.
    assert (await call(tools, "mail_search", unread_only=True, subject="Invoice"))[0]["unread"]


async def test_search_by_text_dates_and_unread(
    tools: ModuleType, greenmail: Any, mail_env: Callable[..., dict[str, str]], make_mail: Any
) -> None:
    greenmail.append(make_mail(subject="One", body="the magic word is pumpernickel"))
    greenmail.append(make_mail(subject="Two", message_id="<2@x>"), seen=True)

    assert [m["subject"] for m in await call(tools, "mail_search", text="pumpernickel")] == ["One"]
    assert [m["subject"] for m in await call(tools, "mail_search", unread_only=True)] == ["One"]
    assert len(await call(tools, "mail_search", since="2000-01-01")) == 2
    assert await call(tools, "mail_search", before="2000-01-01") == []


async def test_search_with_non_ascii_text(
    tools: ModuleType, greenmail: Any, mail_env: Callable[..., dict[str, str]], make_mail: Any
) -> None:
    greenmail.append(make_mail(subject="Grüße aus Köln", message_id="<k@x>"))
    greenmail.append(make_mail(subject="Hello", message_id="<h@x>"))
    # GreenMail matches FROM/TO on whole addresses only.
    found = await call(tools, "mail_search", subject="Grüße", to="agent@example.com")
    assert [m["subject"] for m in found] == ["Grüße aus Köln"]
    with pytest.raises(Exception, match="at most one value containing non-ASCII"):
        await call(tools, "mail_search", subject="Grüße", text="Köln")


async def test_html_only_mail_is_read_as_text(
    tools: ModuleType, greenmail: Any, mail_env: Callable[..., dict[str, str]], make_mail: Any
) -> None:
    greenmail.append(
        make_mail(html="<html><body><p>Hello <b>there</b></p><script>x()</script></body></html>")
    )
    [hit] = await call(tools, "mail_search")
    mail = await call(tools, "mail_get", id=hit["id"])
    assert "Hello there" in mail["text"]
    assert "<b>" not in mail["text"] and "x()" not in mail["text"]


async def test_long_text_is_truncated_with_a_note(
    tools: ModuleType, greenmail: Any, mail_env: Callable[..., dict[str, str]], make_mail: Any
) -> None:
    greenmail.append(make_mail(body="a" * 25_000))
    [hit] = await call(tools, "mail_search")
    mail = await call(tools, "mail_get", id=hit["id"])
    assert mail["text_truncated"] is True
    assert mail["text"].startswith("a" * 20_000)
    assert "a" * 20_001 not in mail["text"]
    assert "20,000" in mail["text"]


async def test_attachment_round_trip(
    tools: ModuleType, greenmail: Any, mail_env: Callable[..., dict[str, str]], make_mail: Any
) -> None:
    pdf = b"%PDF-1.4 binary \x00\x01\x02" * 50
    greenmail.append(
        make_mail(
            attachments=[
                ("notes.txt", "text/plain", b"line one\nline two\n"),
                ("invoice.pdf", "application/pdf", pdf),
            ]
        )
    )
    [hit] = await call(tools, "mail_search")
    mail = await call(tools, "mail_get", id=hit["id"])
    assert mail["attachments"] == [
        {"id": "1", "name": "notes.txt", "type": "text/plain", "size": 18},
        {"id": "2", "name": "invoice.pdf", "type": "application/pdf", "size": len(pdf)},
    ]
    assert "line one" not in mail["text"]

    text = await call(tools, "mail_get_attachment", id=hit["id"], attachment_id="1")
    assert text["encoding"] == "text" and text["content"] == "line one\nline two\n"
    binary = await call(tools, "mail_get_attachment", id=hit["id"], attachment_id="2")
    assert binary["name"] == "invoice.pdf" and binary["size"] == len(pdf)
    assert binary["encoding"] == "base64"
    assert base64.b64decode(binary["content"]) == pdf

    with pytest.raises(Exception, match="no attachment 3"):
        await call(tools, "mail_get_attachment", id=hit["id"], attachment_id="3")


async def test_an_unknown_or_stale_id_is_a_clear_error(
    tools: ModuleType, greenmail: Any, mail_env: Callable[..., dict[str, str]], make_mail: Any
) -> None:
    greenmail.append(make_mail())
    [hit] = await call(tools, "mail_search")
    folder, uidvalidity, uid = hit["id"].rsplit(":", 2)
    with pytest.raises(Exception, match="no longer valid"):
        await call(tools, "mail_get", id=f"{folder}:{int(uidvalidity) + 1}:{uid}")
    with pytest.raises(Exception, match="not found"):
        await call(tools, "mail_get", id=f"{folder}:{uidvalidity}:{int(uid) + 100}")
    with pytest.raises(Exception, match="not a valid mail id"):
        await call(tools, "mail_get", id="nonsense")


async def test_list_folders(
    tools: ModuleType, greenmail: Any, mail_env: Callable[..., dict[str, str]]
) -> None:
    conn = greenmail.imap()
    conn.create('"Archiv-&AMQ-"')  # "Archiv-Ä" in IMAP's modified UTF-7
    conn.logout()
    names = [f["name"] for f in await call(tools, "mail_list_folders")]
    assert "INBOX" in names and "Archiv-Ä" in names


# --- changing -----------------------------------------------------------------


async def test_move_mark_read_and_delete(
    tools: ModuleType, greenmail: Any, mail_env: Callable[..., dict[str, str]], make_mail: Any
) -> None:
    conn = greenmail.imap()
    conn.create("Done")
    conn.logout()
    greenmail.append(make_mail(subject="keep", message_id="<k@x>"))
    greenmail.append(make_mail(subject="drop", message_id="<d@x>"))
    hits = {m["subject"]: m["id"] for m in await call(tools, "mail_search")}

    marked = await call(tools, "mail_mark_read", id=hits["keep"])
    assert marked["status"] == "marked read"
    assert await call(tools, "mail_search", unread_only=True, subject="keep") == []
    await call(tools, "mail_mark_read", id=hits["keep"], read=False)
    assert len(await call(tools, "mail_search", unread_only=True, subject="keep")) == 1

    moved = await call(tools, "mail_move", id=hits["keep"], folder="Done")
    assert moved["status"] == "moved" and moved["folder"] == "Done"
    assert [m["subject"] for m in await call(tools, "mail_search", folder="Done")] == ["keep"]
    in_done = (await call(tools, "mail_search", folder="Done"))[0]["id"]
    if moved.get("id"):
        assert moved["id"] == in_done
    assert [m["subject"] for m in await call(tools, "mail_search")] == ["drop"]

    deleted = await call(tools, "mail_delete", id=hits["drop"])
    assert deleted["status"] == "deleted"
    assert await call(tools, "mail_search") == []

    with pytest.raises(Exception, match="does not exist"):
        await call(tools, "mail_move", id=in_done, folder="Nowhere")


# --- sending ----------------------------------------------------------------


async def test_send_with_an_attachment(
    tools: ModuleType, greenmail: Any, mail_env: Callable[..., dict[str, str]]
) -> None:
    result = await call(
        tools,
        "mail_send",
        to=["alice@example.com"],
        cc=["Bob <bob@example.com>"],
        subject="Report",
        body="Here it is.",
        attachments=[
            {"name": "report.csv", "text": "a,b\n1,2\n"},
            {"name": "logo.png", "content_base64": base64.b64encode(b"\x89PNG..").decode()},
        ],
    )
    assert result["status"] == "sent" and result["message_id"].startswith("<")

    [raw] = greenmail.messages(user={"login": "alice", "password": "alice-pw-123"})
    msg = parsed(raw)
    assert msg["From"] == "Agent Smith <agent@example.com>"
    assert msg["To"] == "alice@example.com"
    assert msg["Cc"] == "Bob <bob@example.com>"
    assert msg["Auto-Submitted"] == "auto-replied"
    assert msg["Message-ID"] == result["message_id"]
    body = msg.get_body(preferencelist=("plain",))
    assert body is not None and body.get_content().strip() == "Here it is."
    parts = {p.get_filename(): p for p in msg.iter_attachments()}
    assert parts["report.csv"].get_content_type() == "text/csv"
    assert parts["report.csv"].get_payload(decode=True) == b"a,b\n1,2\n"
    assert parts["logo.png"].get_payload(decode=True) == b"\x89PNG.."
    assert len(greenmail.messages(user={"login": "bob", "password": "bob-pw-123"})) == 1


async def test_reply_keeps_the_thread_and_marks_itself_automatic(
    tools: ModuleType, greenmail: Any, mail_env: Callable[..., dict[str, str]], make_mail: Any
) -> None:
    greenmail.append(
        make_mail(
            subject="Question",
            message_id="<q1@example.com>",
            to="agent@example.com, bob@example.com",
            headers={"References": "<q0@example.com>", "Cc": "carol@example.com"},
        )
    )
    [hit] = await call(tools, "mail_search")
    result = await call(tools, "mail_reply", id=hit["id"], body="Answer.", reply_all=True)
    assert result["status"] == "sent"

    [raw] = greenmail.messages(user={"login": "alice", "password": "alice-pw-123"})
    msg = parsed(raw)
    assert msg["Subject"] == "Re: Question"
    assert msg["In-Reply-To"] == "<q1@example.com>"
    assert msg["References"] == "<q0@example.com> <q1@example.com>"
    assert msg["Auto-Submitted"] == "auto-replied"
    assert msg["To"] == "Alice <alice@example.com>"
    # Reply-all keeps the others and leaves the mailbox itself out.
    assert msg["Cc"] == "bob@example.com, carol@example.com"
    body = msg.get_body(preferencelist=("plain",))
    assert body is not None
    text = body.get_content()
    assert text.startswith("Answer.")
    assert "> please have a look." in text


async def test_forward_attaches_the_original(
    tools: ModuleType, greenmail: Any, mail_env: Callable[..., dict[str, str]], make_mail: Any
) -> None:
    greenmail.append(make_mail(subject="FYI", attachments=[("a.txt", "text/plain", b"hi")]))
    [hit] = await call(tools, "mail_search")
    result = await call(
        tools, "mail_forward", id=hit["id"], to=["bob@example.com"], body="See below."
    )
    assert result["status"] == "sent"
    [raw] = greenmail.messages(user={"login": "bob", "password": "bob-pw-123"})
    msg = parsed(raw)
    assert msg["Subject"] == "Fwd: FYI"
    assert msg["Auto-Submitted"] == "auto-replied"
    [original] = [p for p in msg.iter_attachments() if p.get_content_type() == "message/rfc822"]
    inner = original.get_content()
    assert inner["Subject"] == "FYI"


async def test_recipient_and_size_limits(
    tools: ModuleType, greenmail: Any, mail_env: Callable[..., dict[str, str]]
) -> None:
    fifty = [f"r{i}@example.com" for i in range(50)]
    with pytest.raises(Exception, match="at most 50 recipients"):
        await call(tools, "mail_send", to=fifty, cc=["one-more@example.com"], subject="s", body="b")
    big = base64.b64encode(b"x" * (25 * 1024 * 1024 + 1)).decode()
    with pytest.raises(Exception, match="25 MB"):
        await call(
            tools,
            "mail_send",
            to=["alice@example.com"],
            subject="s",
            body="b",
            attachments=[{"name": "big.bin", "content_base64": big}],
        )
    with pytest.raises(Exception, match="not a valid address"):
        await call(tools, "mail_send", to=["not an address"], subject="s", body="b")
    with pytest.raises(Exception, match="line break"):
        await call(tools, "mail_send", to=["alice@example.com"], subject="s\r\nBcc: x@y", body="b")
    assert greenmail.messages(user={"login": "alice", "password": "alice-pw-123"}) == []


async def test_a_too_large_attachment_is_not_read(
    tools: ModuleType,
    mailbox: ModuleType,
    greenmail: Any,
    mail_env: Callable[..., dict[str, str]],
    make_mail: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    greenmail.append(make_mail(attachments=[("big.bin", "application/octet-stream", b"x" * 64)]))
    monkeypatch.setattr(mailbox, "MAX_ATTACHMENT_BYTES", 32)
    [hit] = await call(tools, "mail_search")
    with pytest.raises(Exception, match="larger than"):
        await call(tools, "mail_get_attachment", id=hit["id"], attachment_id="1")


# --- POP3 ---------------------------------------------------------------------


async def test_pop3_reads_searches_and_deletes_by_uidl(
    tools: ModuleType, greenmail: Any, mail_env: Callable[..., dict[str, str]], make_mail: Any
) -> None:
    mail_env(protocol="pop3")
    greenmail.append(make_mail(subject="Pop one", attachments=[("a.txt", "text/plain", b"hi")]))
    greenmail.append(make_mail(subject="Pop two", message_id="<p2@x>"))

    found = await call(tools, "mail_search", subject="one")
    assert [m["subject"] for m in found] == ["Pop one"]
    mail = await call(tools, "mail_get", id=found[0]["id"])
    assert mail["subject"] == "Pop one" and mail["folder"] is None
    att = await call(tools, "mail_get_attachment", id=found[0]["id"], attachment_id="1")
    assert att["content"] == "hi"
    assert len(await call(tools, "mail_search", text="look")) == 2

    await call(tools, "mail_delete", id=found[0]["id"])
    assert [m["subject"] for m in await call(tools, "mail_search")] == ["Pop two"]


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("mail_list_folders", {}),
        ("mail_move", {"id": "x", "folder": "Done"}),
        ("mail_mark_read", {"id": "x"}),
        ("mail_search", {"unread_only": True}),
        ("mail_search", {"folder": "Archive"}),
    ],
)
async def test_pop3_limits_are_explained(
    tools: ModuleType,
    greenmail: Any,
    mail_env: Callable[..., dict[str, str]],
    tool: str,
    args: dict[str, Any],
) -> None:
    mail_env(protocol="pop3")
    with pytest.raises(Exception, match="POP3"):
        await call(tools, tool, **args)


# --- the bridge ---------------------------------------------------------------


async def test_the_bridge_reports_a_tool_error_as_an_error_result(
    bridge: ModuleType, greenmail: Any, mail_env: Callable[..., dict[str, str]]
) -> None:
    import mcp.types as types

    mail_env(password="wrong-password-xyz")
    result = await bridge._on_call_tool(
        None, types.CallToolRequestParams(name="mail_search", arguments={})
    )
    assert result.is_error
    text = result.content[0].text
    assert "username or password" in text
    assert "wrong-password-xyz" not in text

    listed = await bridge._on_list_tools(None, None)
    assert len(listed.tools) == 10


async def test_the_bridge_starts_the_way_core_launches_it(
    tools: ModuleType, mailbox: ModuleType, greenmail: Any, mail_env: Callable[..., dict[str, str]]
) -> None:
    """`python -m mcp_bridge` with the capa folder on PYTHONPATH and the login
    in the environment, through core's own MCP client. Outside this test
    process there is no test trust, so the call proves the real bridge
    verifies the server's certificate."""
    import sys

    from oc8.agent.mcp_client import McpSession

    fields = mail_env()
    env = {name: fields[key] for name, key in mailbox.ENV_FIELDS.items() if key in fields}
    env["PYTHONPATH"] = PLUGIN_ROOT
    async with McpSession(sys.executable, ["-m", "mcp_bridge"], env) as session:
        assert {t.name for t in session.tools} == set(tools.CALL_HANDLERS)
        with pytest.raises(Exception, match="certificate could not be verified") as caught:
            await session.call("mail_search", {})
        assert fields["password"] not in str(caught.value)


# --- hardening ------------------------------------------------------------------


async def test_references_keep_the_first_and_the_last_ten_whole_ids(
    tools: ModuleType, greenmail: Any, mail_env: Callable[..., dict[str, str]], make_mail: Any
) -> None:
    chain = " ".join(f"<r{i}@example.com>" for i in range(30))
    greenmail.append(
        make_mail(
            message_id="<last@example.com>",
            headers={"References": chain + " junk <broken@exa"},
        )
    )
    [hit] = await call(tools, "mail_search")
    await call(tools, "mail_reply", id=hit["id"], body="Answer.")
    [raw] = greenmail.messages(user={"login": "alice", "password": "alice-pw-123"})
    references = str(parsed(raw)["References"]).split()
    assert references == [
        "<r0@example.com>",
        *[f"<r{i}@example.com>" for i in range(21, 30)],
        "<last@example.com>",
    ]


async def test_a_line_break_in_a_recipient_name_is_refused(
    tools: ModuleType, greenmail: Any, mail_env: Callable[..., dict[str, str]]
) -> None:
    with pytest.raises(Exception, match="line break"):
        await call(
            tools,
            "mail_send",
            to=['"Bob\r\nBcc: mallory@example.com" <bob@example.com>'],
            subject="s",
            body="b",
        )
    assert greenmail.messages(user={"login": "bob", "password": "bob-pw-123"}) == []


async def test_a_line_break_in_the_sender_name_stays_on_one_line(
    tools: ModuleType, greenmail: Any, mail_env: Callable[..., dict[str, str]]
) -> None:
    mail_env(from_name="Agent\r\nBcc: mallory@example.com")
    await call(tools, "mail_send", to=["alice@example.com"], subject="s", body="b")
    [raw] = greenmail.messages(user={"login": "alice", "password": "alice-pw-123"})
    msg = parsed(raw)
    assert "Bcc" not in msg and b"\r\nBcc:" not in raw
    assert "\n" not in str(msg["From"]) and str(msg["From"]).endswith("<agent@example.com>")


async def test_fifty_recipients_pass_and_fifty_one_do_not(
    tools: ModuleType, greenmail: Any, mail_env: Callable[..., dict[str, str]]
) -> None:
    fifty = [f"r{i}@example.com" for i in range(50)]
    result = await call(tools, "mail_send", to=fifty, subject="s", body="b")
    assert result["recipients"] == 50
    with pytest.raises(Exception, match="at most 50 recipients; this one has 51"):
        await call(tools, "mail_send", to=[*fifty, "r50@example.com"], subject="s", body="b")


@pytest.mark.parametrize(
    "mime", ["text", "text/plain; charset=x", "text/plain\r\nBcc: x@y", "a/b/c", "te xt/plain"]
)
async def test_an_attachment_type_must_be_a_media_type(
    tools: ModuleType, greenmail: Any, mail_env: Callable[..., dict[str, str]], mime: str
) -> None:
    with pytest.raises(Exception, match="not a media type"):
        await call(
            tools,
            "mail_send",
            to=["alice@example.com"],
            subject="s",
            body="b",
            attachments=[{"name": "a.txt", "text": "x", "type": mime}],
        )
    result = await call(
        tools,
        "mail_send",
        to=["alice@example.com"],
        subject="s",
        body="b",
        attachments=[{"name": "a.bin", "text": "x", "type": "application/vnd.oc8+json"}],
    )
    assert result["status"] == "sent"


async def test_a_reply_to_on_another_domain_is_not_followed(
    tools: ModuleType, greenmail: Any, mail_env: Callable[..., dict[str, str]], make_mail: Any
) -> None:
    greenmail.append(make_mail(headers={"Reply-To": "mallory@evil.example"}))
    [hit] = await call(tools, "mail_search")
    result = await call(tools, "mail_reply", id=hit["id"], body="Answer.")
    assert "Reply-To" in result["note"]
    [raw] = greenmail.messages(user={"login": "alice", "password": "alice-pw-123"})
    assert parsed(raw)["To"] == "Alice <alice@example.com>"


async def test_a_reply_to_on_the_senders_domain_is_followed(
    tools: ModuleType, greenmail: Any, mail_env: Callable[..., dict[str, str]], make_mail: Any
) -> None:
    greenmail.append(make_mail(headers={"Reply-To": "Bob <bob@example.com>"}))
    [hit] = await call(tools, "mail_search")
    result = await call(tools, "mail_reply", id=hit["id"], body="Answer.")
    assert "note" not in result
    assert greenmail.messages(user={"login": "alice", "password": "alice-pw-123"}) == []
    [raw] = greenmail.messages(user={"login": "bob", "password": "bob-pw-123"})
    assert parsed(raw)["To"] == "Bob <bob@example.com>"
