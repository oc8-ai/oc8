from __future__ import annotations

import json
from pathlib import Path

import pytest

# from oc8_evals.mocks import google_workspace as g
from oc8_evals.mocks import microsoft365 as m365
from oc8_evals.mocks._store import Store

pytestmark = pytest.mark.asyncio


def _seed_m365(path: Path) -> Store:
    store = Store(path)
    store.update(
        lambda s: s.update(
            {
                "messages": [
                    {
                        "id": "msg-1",
                        "from": "carol@example.com",
                        "to": ["me@example.com"],
                        "subject": "Invoice question",
                        "body": "Where is invoice 42?",
                        "folder": "inbox",
                        "inReplyTo": None,
                        "sent": False,
                    }
                ],
                "busy": {"alice@example.com": [["2026-09-28T09:00:00", "2026-09-28T12:00:00"]]},
            }
        )
    )
    return store


async def test_m365_search_get_and_draft_reply(tmp_path: Path) -> None:
    store = _seed_m365(tmp_path / "s.json")
    handlers = m365.handlers(store)
    found = json.loads(await handlers["mail_search"]({"query": "invoice"}))
    assert [msg["id"] for msg in found] == ["msg-1"]
    one = json.loads(await handlers["mail_get"]({"messageId": "msg-1"}))
    assert one["subject"] == "Invoice question"
    await handlers["mail_create_draft"](
        {
            "to": ["carol@example.com"],
            "subject": "Re: Invoice question",
            "body": "Attached.",
            "inReplyTo": "msg-1",
        }
    )
    state = store.read()
    assert len(state["drafts"]) == 1 and state["drafts"][0]["inReplyTo"] == "msg-1"
    assert all(not msg["sent"] for msg in state["messages"])


async def test_m365_send_and_reply_are_recorded_as_sent(tmp_path: Path) -> None:
    store = _seed_m365(tmp_path / "s.json")
    handlers = m365.handlers(store)
    await handlers["mail_send"]({"to": ["bob@example.com"], "subject": "s", "body": "b"})
    await handlers["mail_reply"]({"messageId": "msg-1", "body": "r"})
    sent = [msg for msg in store.read()["messages"] if msg["sent"]]
    assert len(sent) == 2
    assert sent[1]["inReplyTo"] == "msg-1" and sent[1]["to"] == ["carol@example.com"]


async def test_m365_calendar_free_busy_and_create(tmp_path: Path) -> None:
    store = _seed_m365(tmp_path / "s.json")
    handlers = m365.handlers(store)
    fb = json.loads(
        await handlers["calendar_check_free_busy"](
            {
                "attendees": ["alice@example.com", "bob@example.com"],
                "start": "2026-09-28T08:00:00",
                "end": "2026-09-28T18:00:00",
            }
        )
    )
    assert fb["alice@example.com"] == [["2026-09-28T09:00:00", "2026-09-28T12:00:00"]]
    assert fb["bob@example.com"] == []
    await handlers["calendar_create_event"](
        {
            "subject": "Sync",
            "start": "2026-09-28T14:00:00",
            "end": "2026-09-28T15:00:00",
            "attendees": ["alice@example.com", "bob@example.com"],
        }
    )
    listed = json.loads(
        await handlers["calendar_list_events"](
            {"start": "2026-09-28T00:00:00", "end": "2026-09-29T00:00:00"}
        )
    )
    assert len(listed) == 1 and listed[0]["subject"] == "Sync"
    event = json.loads(await handlers["calendar_get_event"]({"eventId": listed[0]["id"]}))
    assert event["attendees"] == ["alice@example.com", "bob@example.com"]


async def test_m365_unknown_message_is_an_error(tmp_path: Path) -> None:
    handlers = m365.handlers(_seed_m365(tmp_path / "s.json"))
    with pytest.raises(KeyError):
        await handlers["mail_get"]({"messageId": "nope"})


def test_m365_tool_names_match_the_real_tool_pack() -> None:
    names = {t.name for t in m365.TOOLS}
    assert names == {
        "mail_search",
        "mail_get",
        "mail_send",
        "mail_reply",
        "mail_create_draft",
        "calendar_list_events",
        "calendar_get_event",
        "calendar_create_event",
        "calendar_check_free_busy",
    }
