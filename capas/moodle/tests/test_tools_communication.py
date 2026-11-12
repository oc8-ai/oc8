from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

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
from mcp_bridge.tools import communication  # noqa: E402

sys.path.remove(str(PLUGIN_ROOT))
_evict()


def test_declares_the_three_communication_tools() -> None:
    names = {t.name for t in communication.TOOLS}
    assert names == {
        "moodle_list_forum_discussions",
        "moodle_send_message",
        "moodle_post_to_forum",
    }
    assert set(communication.CALL_HANDLERS) == names


@pytest.mark.asyncio
async def test_discussions_of_a_forum(fake_moodle: Callable[[Any], Any]) -> None:
    fake = fake_moodle(client)
    fake.on(
        "mod_forum_get_forum_discussions",
        {
            "discussions": [
                {
                    "id": 70,
                    "discussion": 30,
                    "name": "Frage zu Woche 1",
                    "userfullname": "Ana",
                    "created": 1767225600,
                    "timemodified": 1767225600,
                    "numreplies": 2,
                    "message": "<p>Wie lang soll das Referat sein?</p>",
                },
            ]
        },
    )
    result = await communication.moodle_list_forum_discussions({"forum_id": 4})
    assert fake.params("mod_forum_get_forum_discussions") == {
        "forumid": 4,
        "page": 0,
        "perpage": 200,
    }
    assert result["items"] == [
        {
            "id": 30,
            "post_id": 70,
            "subject": "Frage zu Woche 1",
            "author": "Ana",
            "created": "2026-01-01T00:00:00Z",
            "replies": 2,
            "last_post_at": "2026-01-01T00:00:00Z",
            "message": "Wie lang soll das Referat sein?",
        }
    ]


@pytest.mark.asyncio
async def test_posts_of_a_discussion(fake_moodle: Callable[[Any], Any]) -> None:
    fake = fake_moodle(client)
    fake.on(
        "mod_forum_get_discussion_posts",
        {
            "posts": [
                {
                    "id": 70,
                    "subject": "Frage",
                    "author": {"fullname": "Ana"},
                    "timecreated": 1767225600,
                    "parentid": 0,
                    "message": "<p>Wie lang?</p>",
                },
            ]
        },
    )
    result = await communication.moodle_list_forum_discussions({"discussion_id": 30})
    assert fake.params("mod_forum_get_discussion_posts") == {"discussionid": 30}
    assert result["items"][0] == {
        "id": 70,
        "subject": "Frage",
        "author": "Ana",
        "created": "2026-01-01T00:00:00Z",
        "parent_id": 0,
        "message": "Wie lang?",
    }


@pytest.mark.asyncio
async def test_listing_needs_a_forum_or_discussion() -> None:
    with pytest.raises(ValueError, match="forum_id"):
        await communication.moodle_list_forum_discussions({})


@pytest.mark.asyncio
async def test_send_message_reports_failed_recipients(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on(
        "core_message_send_instant_messages",
        [{"msgid": 101}, {"msgid": -1, "errormessage": "User blocked messages"}],
    )
    result = await communication.moodle_send_message(
        {"user_ids": [1, 2], "text": "Bitte das Referat bis Freitag abgeben."}
    )
    assert fake.params("core_message_send_instant_messages") == {
        "messages": [
            {"touserid": 1, "text": "Bitte das Referat bis Freitag abgeben.", "textformat": 2},
            {"touserid": 2, "text": "Bitte das Referat bis Freitag abgeben.", "textformat": 2},
        ]
    }
    assert result == {"sent": 1, "failed": [{"user_id": 2, "error": "User blocked messages"}]}


@pytest.mark.asyncio
async def test_send_message_sends_each_recipient_once(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on(
        "core_message_send_instant_messages",
        lambda params: [{"msgid": 100 + i} for i, _ in enumerate(params["messages"])],
    )
    result = await communication.moodle_send_message(
        {"user_ids": [2, 1, 2, *([1] * 60)], "text": "Erinnerung"}
    )
    sent = fake.params("core_message_send_instant_messages")["messages"]
    assert [m["touserid"] for m in sent] == [2, 1]
    assert result == {"sent": 2, "failed": []}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"user_ids": [], "text": "x"}, "at least one"),
        ({"user_ids": list(range(51)), "text": "x"}, "At most 50"),
        ({"user_ids": [1], "text": "  "}, "empty"),
    ],
)
async def test_send_message_validates(args: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        await communication.moodle_send_message(args)


@pytest.mark.asyncio
async def test_new_discussion(fake_moodle: Callable[[Any], Any]) -> None:
    fake = fake_moodle(client)
    fake.on("mod_forum_add_discussion", {"discussionid": 31, "warnings": []})
    result = await communication.moodle_post_to_forum(
        {"forum_id": 4, "subject": "Erinnerung", "message": "Abgabe Freitag.\nDanke!"}
    )
    assert fake.params("mod_forum_add_discussion") == {
        "forumid": 4,
        "subject": "Erinnerung",
        "message": "Abgabe Freitag.<br>Danke!",
    }
    assert result == {"created": True, "discussion_id": 31}


@pytest.mark.asyncio
async def test_reply(fake_moodle: Callable[[Any], Any]) -> None:
    fake = fake_moodle(client)
    fake.on("mod_forum_add_discussion_post", {"postid": 71, "warnings": []})
    result = await communication.moodle_post_to_forum({"post_id": 70, "message": "Ca. 10 Min."})
    assert fake.params("mod_forum_add_discussion_post") == {
        "postid": 70,
        "subject": "Re:",
        "message": "Ca. 10 Min.",
    }
    assert result == {"replied": True, "post_id": 71}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"forum_id": 4, "post_id": 70, "message": "x", "subject": "s"}, "not both"),
        ({"message": "x"}, "forum_id"),
        ({"forum_id": 4, "message": "x"}, "needs a subject"),
        ({"post_id": 70, "message": ""}, "empty"),
    ],
)
async def test_post_to_forum_validates(args: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        await communication.moodle_post_to_forum(args)


@pytest.mark.asyncio
@pytest.mark.parametrize("replies", [[{"msgid": 101}], [], None])
async def test_send_message_flags_recipients_without_a_reply(
    fake_moodle: Callable[[Any], Any], replies: Any
) -> None:
    fake = fake_moodle(client)
    fake.on("core_message_send_instant_messages", replies)
    result = await communication.moodle_send_message({"user_ids": [1, 2], "text": "Hallo"})
    missing = [{"user_id": 2, "error": "Moodle returned no result for this recipient"}]
    if replies:
        assert result == {"sent": 1, "failed": missing}
    else:
        assert result["sent"] == 0
        assert [f["user_id"] for f in result["failed"]] == [1, 2]


@pytest.mark.asyncio
async def test_a_full_discussion_batch_is_flagged_as_capped(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on(
        "mod_forum_get_forum_discussions",
        {"discussions": [{"id": i, "discussion": i, "name": "t"} for i in range(200)]},
    )
    result = await communication.moodle_list_forum_discussions({"forum_id": 4})
    assert result["truncated"] is True
    assert "200 most recent" in result["note"]


@pytest.mark.asyncio
async def test_a_small_discussion_batch_has_no_note(fake_moodle: Callable[[Any], Any]) -> None:
    fake = fake_moodle(client)
    fake.on(
        "mod_forum_get_forum_discussions",
        {"discussions": [{"id": i, "discussion": i, "name": "t"} for i in range(3)]},
    )
    result = await communication.moodle_list_forum_discussions({"forum_id": 4})
    assert "note" not in result
