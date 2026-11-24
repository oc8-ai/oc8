"""0130: chat messages form a tree (`parent_id`) and a chat remembers the leaf
the person is looking at (`current_leaf_id`); existing chats are backfilled."""

from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config

from oc8.models._mixins import uuid7


def _cfg() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def _columns(dsn: str, table: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,)
        ).fetchall()
    return {row[0] for row in rows}


def test_upgrade_adds_the_columns_and_downgrade_removes_them(pg_url: str) -> None:
    dsn = pg_url.replace("+psycopg", "")
    cfg = _cfg()
    command.downgrade(cfg, "0129")
    try:
        assert "parent_id" not in _columns(dsn, "chat_message")
        assert "current_leaf_id" not in _columns(dsn, "chat_session")
    finally:
        command.upgrade(cfg, "head")
    assert "parent_id" in _columns(dsn, "chat_message")
    assert "current_leaf_id" in _columns(dsn, "chat_session")
    with psycopg.connect(dsn) as conn:
        assert conn.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = 'ix_chat_message_parent_id'"
        ).fetchone()


def test_backfill_links_a_linear_chat_and_keeps_regenerated_variants_siblings(
    pg_url: str,
) -> None:
    dsn = pg_url.replace("+psycopg", "")
    cfg = _cfg()
    tenant, chat, live = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    # uuid7s minted within one millisecond are not ordered; sort so id order is creation order.
    order = ("U1", "A1", "U2", "A2a", "A2b", "U3", "A3", "L1", "L2")
    ids = dict(zip(order, sorted(uuid7() for _ in order), strict=True))
    rows = [
        ("U1", chat, "user", None),
        ("A1", chat, "assistant", "U1"),
        ("U2", chat, "user", None),
        ("A2a", chat, "assistant", "U2"),
        ("A2b", chat, "assistant", "U2"),
        ("U3", chat, "user", None),
        ("A3", chat, "assistant", "U3"),
        ("L1", live, "user", None),
        ("L2", live, "assistant", "L1"),
    ]
    command.downgrade(cfg, "0129")
    try:
        with psycopg.connect(dsn) as conn:
            for sid, kind in ((chat, "chat"), (live, "live")):
                conn.execute(
                    "INSERT INTO chat_session (id, tenant_id, agent_id, member_id, kind) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (sid, tenant, uuid.uuid4(), uuid.uuid4(), kind),
                )
            for name, sid, role, reply in rows:
                conn.execute(
                    "INSERT INTO chat_message (id, tenant_id, session_id, role, reply_to_id) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (ids[name], tenant, sid, role, ids[reply] if reply else None),
                )
    finally:
        command.upgrade(cfg, "head")
    names = {v: k for k, v in ids.items()}
    with psycopg.connect(dsn) as conn:
        parents = {
            names[i]: (names[p] if p else None)
            for i, p in conn.execute(
                "SELECT id, parent_id FROM chat_message WHERE tenant_id = %s", (tenant,)
            ).fetchall()
        }
        leaves = dict(
            conn.execute(
                "SELECT id, current_leaf_id FROM chat_session WHERE tenant_id = %s", (tenant,)
            ).fetchall()
        )
    assert parents == {
        "U1": None,
        "A1": "U1",
        "U2": "A1",
        "A2a": "U2",
        "A2b": "U2",
        "U3": "A2b",
        "A3": "U3",
        "L1": None,
        "L2": None,
    }
    assert leaves[chat] == ids["A3"]
    assert leaves[live] is None


def _backfill(
    pg_url: str, rows: list[tuple[str, str, str | None]], empty_chat: bool = False
) -> tuple[dict[str, str | None], dict[str, str | None], uuid.UUID | None]:
    """Seed one chat at 0129 from (name, role, reply_to name) rows, upgrade, and
    return its parent map, the leaf name and the empty chat's leaf."""
    dsn = pg_url.replace("+psycopg", "")
    cfg = _cfg()
    tenant, chat, empty = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    order = [r[0] for r in rows]
    ids = dict(zip(order, sorted(uuid7() for _ in order), strict=True))
    command.downgrade(cfg, "0129")
    try:
        with psycopg.connect(dsn) as conn:
            for sid in (chat, empty):
                conn.execute(
                    "INSERT INTO chat_session (id, tenant_id, agent_id, member_id, kind) "
                    "VALUES (%s, %s, %s, %s, 'chat')",
                    (sid, tenant, uuid.uuid4(), uuid.uuid4()),
                )
            for name, role, reply in rows:
                conn.execute(
                    "INSERT INTO chat_message (id, tenant_id, session_id, role, reply_to_id) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (ids[name], tenant, chat, role, ids[reply] if reply else None),
                )
    finally:
        command.upgrade(cfg, "head")
    names = {v: k for k, v in ids.items()}
    with psycopg.connect(dsn) as conn:
        parents = {
            names[i]: (names[p] if p else None)
            for i, p in conn.execute(
                "SELECT id, parent_id FROM chat_message WHERE tenant_id = %s", (tenant,)
            ).fetchall()
        }
        leaves = dict(
            conn.execute(
                "SELECT id, current_leaf_id FROM chat_session WHERE tenant_id = %s", (tenant,)
            ).fetchall()
        )
    leaf = leaves[chat]
    return parents, {"leaf": names[leaf] if leaf else None}, leaves[empty]


def test_an_older_answer_regenerated_later_does_not_hide_what_followed(pg_url: str) -> None:
    parents, leaf, _ = _backfill(
        pg_url,
        [
            ("U1", "user", None),
            ("A1", "assistant", "U1"),
            ("U2", "user", None),
            ("A2", "assistant", "U2"),
            ("A1b", "assistant", "U1"),
            ("U3", "user", None),
            ("A3", "assistant", "U3"),
        ],
    )
    assert parents == {
        "U1": None,
        "A1": "U1",
        "U2": "A1",
        "A2": "U2",
        "A1b": "U1",
        "U3": "A2",
        "A3": "U3",
    }
    assert leaf == {"leaf": "A3"}


def test_a_followup_between_variants_stays_on_the_path(pg_url: str) -> None:
    parents, leaf, _ = _backfill(
        pg_url,
        [
            ("U2", "user", None),
            ("A2a", "assistant", "U2"),
            ("F", "followup", None),
            ("A2b", "assistant", "U2"),
        ],
    )
    assert parents == {"U2": None, "A2a": "U2", "F": "A2a", "A2b": "U2"}
    assert leaf == {"leaf": "F"}


def test_an_assistant_row_without_reply_to_just_follows_the_previous_row(pg_url: str) -> None:
    parents, leaf, _ = _backfill(
        pg_url,
        [("U1", "user", None), ("A1", "assistant", None), ("A2", "assistant", None)],
    )
    assert parents == {"U1": None, "A1": "U1", "A2": "A1"}
    assert leaf == {"leaf": "A2"}


def test_a_chat_without_messages_has_no_leaf(pg_url: str) -> None:
    _, _, empty_leaf = _backfill(pg_url, [("U1", "user", None)])
    assert empty_leaf is None
