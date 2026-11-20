"""The `mail.received` poller against GreenMail: baseline, new mail, a reset
folder, loop protection, truncation, and the summary the agent gets."""

from __future__ import annotations

from collections.abc import Callable
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.usefixtures("trust_test_certificates", "allow_loopback")

INBOX = {"folder": "INBOX"}


async def baseline(source: ModuleType, fields: dict[str, str], config: Any = INBOX) -> Any:
    result = await source.poll(fields, config, None)
    assert result.events == []
    return result.next_cursor


async def test_the_first_poll_takes_a_baseline_and_fires_nothing(
    source: ModuleType, greenmail: Any, make_mail: Any
) -> None:
    for i in range(3):
        greenmail.append(make_mail(message_id=f"<old{i}@x>"))
    fields = greenmail.fields()
    cursor = await baseline(source, fields)
    assert set(cursor) == {"uidvalidity", "last_uid"}
    assert cursor["last_uid"] >= 3

    again = await source.poll(fields, INBOX, cursor)
    assert again.events == [] and again.next_cursor == cursor


async def test_new_mail_becomes_one_event_with_payload_and_summary(
    source: ModuleType,
    tools: ModuleType,
    greenmail: Any,
    make_mail: Any,
    mail_env: Callable[..., dict[str, str]],
) -> None:
    fields = greenmail.fields()
    cursor = await baseline(source, fields)
    greenmail.append(
        make_mail(
            subject="Order 4711",
            message_id="<order-4711@shop.example>",
            headers={"Cc": "bob@example.com"},
            body="Please ship it today.",
            attachments=[("order.pdf", "application/pdf", b"%PDF" * 300)],
        )
    )

    result = await source.poll(fields, INBOX, cursor)
    [event] = result.events
    assert event.delivery_id == "<order-4711@shop.example>"
    payload = event.payload
    assert payload["folder"] == "INBOX"
    assert payload["message_id"] == "<order-4711@shop.example>"
    assert payload["from"] == "Alice <alice@example.com>"
    assert payload["to"] == "agent@example.com"
    assert payload["cc"] == "bob@example.com"
    assert payload["subject"] == "Order 4711"
    assert payload["date"] == "Fri, 20 Nov 2026 10:00:00 +0000"
    assert payload["attachments"] == [
        {"id": "1", "name": "order.pdf", "type": "application/pdf", "size": 1200}
    ]
    assert result.next_cursor["last_uid"] == int(payload["id"].rsplit(":", 1)[1])

    summary = event.summary
    assert f"Mail id: {payload['id']}" in summary
    for line in (
        "From: Alice <alice@example.com>",
        "To: agent@example.com",
        "Cc: bob@example.com",
        "Date: Fri, 20 Nov 2026 10:00:00 +0000",
        "Subject: Order 4711",
        "Message-ID: <order-4711@shop.example>",
        "Please ship it today.",
        "- attachment id 1: order.pdf (application/pdf, 1.2 KB)",
    ):
        assert line in summary
    assert "mail_get_attachment" in summary

    # The ids in the summary work with the tools.
    mail = await tools.CALL_HANDLERS["mail_get"]({"id": payload["id"]})
    assert mail["subject"] == "Order 4711"
    attachment = await tools.CALL_HANDLERS["mail_get_attachment"](
        {"id": payload["id"], "attachment_id": "1"}
    )
    assert attachment["name"] == "order.pdf"

    # Polling never marks mail read.
    assert mail["unread"] is True
    # Polled once, fired once: the cursor moved past it.
    assert (await source.poll(fields, INBOX, result.next_cursor)).events == []


async def test_a_mail_without_message_id_is_keyed_by_uidvalidity_and_uid(
    source: ModuleType, greenmail: Any, make_mail: Any
) -> None:
    fields = greenmail.fields()
    cursor = await baseline(source, fields)
    greenmail.append(make_mail(message_id=None))
    [event] = (await source.poll(fields, INBOX, cursor)).events
    _, uidvalidity, uid = event.payload["id"].rsplit(":", 2)
    assert event.delivery_id == f"{uidvalidity}:{uid}"
    assert event.payload["message_id"] == ""


async def test_a_changed_uidvalidity_takes_a_new_baseline(
    source: ModuleType, greenmail: Any, make_mail: Any
) -> None:
    conn = greenmail.imap()
    conn.create("Projects")
    conn.logout()
    config = {"folder": "Projects"}
    fields = greenmail.fields()
    greenmail.append(make_mail(), folder="Projects")
    cursor = await baseline(source, fields, config)

    # The server reset the folder (GreenMail cannot be made to, so the stored
    # position names another UIDVALIDITY): its old UIDs mean nothing, and the
    # mail now in it is not new to anyone.
    for i in range(2):
        greenmail.append(make_mail(message_id=f"<n{i}@x>"), folder="Projects")
    stale = {"uidvalidity": cursor["uidvalidity"] + 1, "last_uid": 0}
    result = await source.poll(fields, config, stale)
    assert result.events == []
    assert result.next_cursor["uidvalidity"] == cursor["uidvalidity"]
    assert result.next_cursor["last_uid"] >= cursor["last_uid"] + 2

    # From the new position on, mail is new again.
    greenmail.append(make_mail(message_id="<after@x>"), folder="Projects")
    [event] = (await source.poll(fields, config, result.next_cursor)).events
    assert event.delivery_id == "<after@x>"
    assert event.payload["folder"] == "Projects"


async def test_a_malformed_cursor_takes_a_new_baseline(
    source: ModuleType, greenmail: Any, make_mail: Any
) -> None:
    greenmail.append(make_mail())
    fields = greenmail.fields()
    result = await source.poll(fields, INBOX, {"something": "else"})
    assert result.events == [] and set(result.next_cursor) == {"uidvalidity", "last_uid"}


@pytest.mark.parametrize(
    ("headers", "sender"),
    [
        ({}, "Agent Smith <agent@example.com>"),
        ({}, "AGENT@example.com"),
        ({"Auto-Submitted": "auto-replied"}, None),
        ({"Auto-Submitted": "auto-generated; owner-email=x@y"}, None),
        ({"Precedence": "bulk"}, None),
        ({"Precedence": "junk"}, None),
        ({"Precedence": "List"}, None),
    ],
)
async def test_loop_protection_skips_automatic_mail_and_moves_on(
    source: ModuleType,
    greenmail: Any,
    make_mail: Any,
    headers: dict[str, str],
    sender: str | None,
) -> None:
    fields = greenmail.fields()
    cursor = await baseline(source, fields)
    kwargs: dict[str, Any] = {"headers": headers, "message_id": "<skip@x>"}
    if sender:
        kwargs["sender"] = sender
    greenmail.append(make_mail(**kwargs))
    greenmail.append(make_mail(message_id="<real@x>"))

    result = await source.poll(fields, INBOX, cursor)
    assert [e.delivery_id for e in result.events] == ["<real@x>"]
    assert (await source.poll(fields, INBOX, result.next_cursor)).events == []


async def test_a_delivery_report_is_skipped(source: ModuleType, greenmail: Any) -> None:
    fields = greenmail.fields()
    cursor = await baseline(source, fields)
    report = (
        b"From: MAILER-DAEMON@example.com\r\nTo: agent@example.com\r\n"
        b"Subject: Undelivered Mail\r\nMessage-ID: <dsn@x>\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/report; report-type=delivery-status; boundary="b"\r\n\r\n'
        b"--b\r\nContent-Type: text/plain\r\n\r\nIt bounced.\r\n"
        b"--b\r\nContent-Type: message/delivery-status\r\n\r\nReporting-MTA: dns; x\r\n\r\n"
        b"--b--\r\n"
    )
    greenmail.append(report)
    result = await source.poll(fields, INBOX, cursor)
    assert result.events == []
    assert result.next_cursor["last_uid"] > cursor["last_uid"]


async def test_auto_submitted_no_is_a_normal_mail(
    source: ModuleType, greenmail: Any, make_mail: Any
) -> None:
    fields = greenmail.fields()
    cursor = await baseline(source, fields)
    greenmail.append(make_mail(headers={"Auto-Submitted": "no"}))
    assert len((await source.poll(fields, INBOX, cursor)).events) == 1


async def test_the_summary_text_is_truncated_with_a_note(
    source: ModuleType, greenmail: Any, make_mail: Any
) -> None:
    fields = greenmail.fields()
    cursor = await baseline(source, fields)
    greenmail.append(make_mail(body="word " * 6_000))
    [event] = (await source.poll(fields, INBOX, cursor)).events
    assert "word " * 4_000 in event.summary
    assert "word " * 4_001 not in event.summary
    assert "truncated" in event.summary and "20,000" in event.summary


async def test_header_values_cannot_add_lines_to_the_summary(
    source: ModuleType, greenmail: Any, make_mail: Any
) -> None:
    fields = greenmail.fields()
    cursor = await baseline(source, fields)
    greenmail.append(make_mail(subject="=?utf-8?q?Hi=0AFrom:_boss@example.com?="))
    [event] = (await source.poll(fields, INBOX, cursor)).events
    assert "Subject: Hi From: boss@example.com" in event.summary
    assert "\nFrom: boss@example.com" not in event.summary


async def test_one_poll_returns_at_most_twenty_mails_and_continues(
    source: ModuleType, greenmail: Any, make_mail: Any
) -> None:
    fields = greenmail.fields()
    cursor = await baseline(source, fields)
    for i in range(23):
        greenmail.append(make_mail(message_id=f"<bulk{i}@x>"))
    first = await source.poll(fields, INBOX, cursor)
    assert [e.delivery_id for e in first.events] == [f"<bulk{i}@x>" for i in range(20)]
    second = await source.poll(fields, INBOX, first.next_cursor)
    assert [e.delivery_id for e in second.events] == [f"<bulk{i}@x>" for i in range(20, 23)]


async def test_a_wrong_password_fails_the_poll_without_the_secret(
    source: ModuleType, greenmail: Any
) -> None:
    secret = "bad-pw-#1"
    with pytest.raises(Exception, match="username or password") as caught:
        await source.poll(greenmail.fields(password=secret), INBOX, None)
    assert secret not in str(caught.value)


async def test_core_refuses_an_internal_mail_server_unless_allowed(
    source: ModuleType, greenmail: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.config import get_settings

    monkeypatch.setattr(get_settings(), "outbound_allowed_private", "")
    with pytest.raises(Exception, match="not reachable from here"):
        await source.poll(greenmail.fields(), INBOX, None)


# --- POP3 ---------------------------------------------------------------------


async def test_pop3_baseline_new_mail_and_seen_list(
    source: ModuleType, greenmail: Any, make_mail: Any
) -> None:
    fields = greenmail.fields(protocol="pop3")
    greenmail.append(make_mail(message_id="<old@x>"))
    cursor = await baseline(source, fields)
    assert list(cursor) == ["seen"] and len(cursor["seen"]) == 1

    greenmail.append(make_mail(message_id="<new@x>", subject="Fresh"))
    greenmail.append(make_mail(message_id="<own@x>", sender="agent@example.com"))
    result = await source.poll(fields, INBOX, cursor)
    [event] = result.events
    assert event.delivery_id == "<new@x>"
    assert event.payload["id"] in result.next_cursor["seen"]
    assert event.payload["folder"] is None
    assert "Subject: Fresh" in event.summary
    assert len(result.next_cursor["seen"]) == 3
    assert (await source.poll(fields, INBOX, result.next_cursor)).events == []


def test_pop3_seen_list_is_capped_and_old_mail_beyond_it_stays_old(
    mailbox: ModuleType,
) -> None:
    listing = [f"u{i}" for i in range(6_000)]
    cursor, new = mailbox.pop3_new_uidls(listing, None, limit=20)
    assert new == [] and cursor["seen"] == listing[-5_000:]

    # u0..u999 fell out of the list but come before it: still old.
    cursor2, new2 = mailbox.pop3_new_uidls([*listing, "n1", "n2"], cursor, limit=20)
    assert new2 == ["n1", "n2"]
    assert len(cursor2["seen"]) == 5_000 and cursor2["seen"][-2:] == ["n1", "n2"]

    # The rest of a burst waits for the next poll.
    many = [*listing, *[f"x{i}" for i in range(30)]]
    cursor3, new3 = mailbox.pop3_new_uidls(many, cursor, limit=20)
    assert new3 == [f"x{i}" for i in range(20)]
    _, new4 = mailbox.pop3_new_uidls(many, cursor3, limit=20)
    assert new4 == [f"x{i}" for i in range(20, 30)]

    # Every seen mail was removed by another client: what is left is new.
    _, new5 = mailbox.pop3_new_uidls(["a", "b"], {"seen": ["gone"]}, limit=20)
    assert new5 == ["a", "b"]
