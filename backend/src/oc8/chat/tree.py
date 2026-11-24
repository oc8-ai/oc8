"""Where a chat message sits in its session's tree (spec §5.3).

Every writer of a `ChatMessage` places it through here, so one rule decides
where it goes: only an edit or a regenerate starts a branch, everything else
appends. A person's write follows the leaf they are looking at; a run's write
follows the user turn it answers (its anchor) and moves the leaf only while
the person is still on that turn's branch -- a late answer never yanks them
out of the branch they are reading.

Only sessions of kind "chat" have a tree. Every other kind (the Live
timeline) stays linear: messages get no parent, the session no leaf, and the
transcript is every message in order, as before.

Each write that reads the leaf locks the session row first (`lock_session`),
so two runs finishing at once, or a send racing a reply, cannot both build on
the same stale leaf; the lock is held until the caller commits.

Lock order -- the one order for every transaction in this area, so no two
of them can wait on each other in a circle. Each takes only the locks it
needs, but never one listed earlier after one listed later:

  1. an approval row (`decide_approval`)
  2. an agent_run row (the executor's claim and every leg end; a decision
     waking its parked run; an answer to a parked question)
  3. a conversation's offer lock (`copilot.direct_access._lock_offers`)
  4. the tenant's audit lock (`lock_tenant_audit`; at the latest at the
     commit of any transaction that staged an audit event)
  5. a run's input lock (`lock_run_inputs`, which takes 4 first)
  6. the chat_session row (`lock_session`, which takes 4 first)

Knowledge rows a chat transaction touches (deleting a session drops its
chat's own knowledge base, `chat.files_kb.drop_chat_kb`) come before step 4,
in `knowledge.tombstone`'s own order (kb_chunk, data_source, knowledge_base),
with one addition: a chat's own `chat_upload` source row is locked first of
all, ahead of kb_chunk -- it is the mutex between that chat's file ingest and
its removal (`chat.files_kb`'s docstring says why that cannot invert). File
rows (`file_attachment`: an edit copying a large file, the ingest setting its
result) come after the knowledge rows and before step 4.

The hazard this guards against: a transaction holding a row from a later
step while it waits for an earlier one -- most often the audit lock, which
every transaction that staged an audit event takes at its commit and a
run's end takes earlier still (`lock_run_inputs`). A send holding the audit
lock and waiting on the session row, against a transaction holding that row
and waiting for the audit lock at its commit, is a deadlock.

So every chat writer takes `lock_session` before it locks or changes the
session row: `send_message` (an edit included), `record_assistant_reply`,
`record_answer_turn`, a carried-over turn, the regenerate door
(`api/v1/chat.py`), a branch switch (`switch_leaf`), and the chat
mode a run entered (`chat.service.apply_entered_mode`); a direct-access
decision takes the steps above in order. A run never locks the session row
during its leg -- that would hold it while the model is called: the
direct-access offer serialises on the offer lock instead, and a mode the run
enters is written at the leg's end. Callers lock late, after their last
model or network call and shortly before they commit, because the audit
lock serialises the tenant's audited commits while held.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from oc8 import models as m
from oc8.audit.chain import lock_tenant_audit
from oc8.chat.audience import session_audience_or_nobody
from oc8.realtime.emit import publish_chat_session_updated


def _has_tree(session: m.ChatSession) -> bool:
    return session.kind == "chat"


async def _all_messages(db: AsyncSession, *, session: m.ChatSession) -> list[m.ChatMessage]:
    stmt = (
        select(m.ChatMessage)
        .where(
            m.ChatMessage.tenant_id == session.tenant_id,
            m.ChatMessage.session_id == session.id,
        )
        .order_by(m.ChatMessage.id.asc())
    )
    return list((await db.execute(stmt)).scalars())


async def _parents(
    db: AsyncSession, *, session: m.ChatSession
) -> dict[uuid.UUID, uuid.UUID | None]:
    """Every message of the session and its parent: the whole tree, ids only."""
    rows = await db.execute(
        select(m.ChatMessage.id, m.ChatMessage.parent_id).where(
            m.ChatMessage.tenant_id == session.tenant_id,
            m.ChatMessage.session_id == session.id,
        )
    )
    return {row.id: row.parent_id for row in rows}


def _ancestry(parents: dict[uuid.UUID, uuid.UUID | None], start: uuid.UUID) -> list[uuid.UUID]:
    """`start` and every message above it, nearest first. Stops at a parent
    that is not in the session (or at a cycle, which no writer creates)."""
    chain: list[uuid.UUID] = []
    current: uuid.UUID | None = start
    while current is not None and current in parents and current not in chain:
        chain.append(current)
        current = parents[current]
    return chain


def _newest_leaf(parents: dict[uuid.UUID, uuid.UUID | None], top: uuid.UUID) -> uuid.UUID:
    """The newest message without children in `top`'s subtree (`top` itself
    when it has none). Ids are uuid7, so the largest is the newest."""
    children: dict[uuid.UUID, list[uuid.UUID]] = {}
    for child, parent in parents.items():
        if parent is not None:
            children.setdefault(parent, []).append(child)
    leaves: list[uuid.UUID] = []
    stack = [top]
    while stack:
        node = stack.pop()
        below = children.get(node)
        if below:
            stack.extend(below)
        else:
            leaves.append(node)
    return max(leaves)


async def lock_session(db: AsyncSession, *, session: m.ChatSession) -> uuid.UUID | None:
    """Take the tenant's audit lock, then the session row lock, until commit
    (see the module docstring for why in that order); both are reentrant.
    Returns the session's leaf as the database has it."""
    await lock_tenant_audit(db, session.tenant_id)
    leaf = (
        await db.execute(
            select(m.ChatSession.current_leaf_id)
            .where(m.ChatSession.id == session.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    set_committed_value(session, "current_leaf_id", leaf)
    return leaf


async def _set_leaf(db: AsyncSession, *, session: m.ChatSession, leaf: uuid.UUID | None) -> None:
    if session.current_leaf_id == leaf:
        return
    session.current_leaf_id = leaf
    await db.flush()
    publish_chat_session_updated(
        db, session=session, audience=await session_audience_or_nobody(db, session=session)
    )


async def _place(db: AsyncSession, *, message: m.ChatMessage, parent: uuid.UUID | None) -> None:
    message.parent_id = parent
    db.add(message)
    await db.flush()


async def visible_path(db: AsyncSession, *, session: m.ChatSession) -> list[m.ChatMessage]:
    """The conversation the person is looking at: root to `current_leaf_id`.
    Every message in order for a linear session, and -- defensively -- for a
    chat whose leaf is unset or no longer exists."""
    messages = await _all_messages(db, session=session)
    leaf = session.current_leaf_id
    if not _has_tree(session) or leaf is None:
        return messages
    by_id = {msg.id: msg for msg in messages}
    if leaf not in by_id:
        return messages
    chain = _ancestry({msg.id: msg.parent_id for msg in messages}, leaf)
    return [by_id[message_id] for message_id in reversed(chain)]


async def path_before(
    db: AsyncSession, *, session: m.ChatSession, message: m.ChatMessage
) -> list[m.ChatMessage]:
    """What came before `message` on its own path, root first -- the
    transcript of a turn that is not (or not yet) where the person is looking:
    a regenerated answer's turn, a carried-over turn placed off the leaf.
    Every earlier message for a linear session."""
    messages = await _all_messages(db, session=session)
    if not _has_tree(session) or session.current_leaf_id is None:
        return [msg for msg in messages if msg.id < message.id]
    by_id = {msg.id: msg for msg in messages}
    if message.parent_id is None:
        return []
    chain = _ancestry({msg.id: msg.parent_id for msg in messages}, message.parent_id)
    return [by_id[message_id] for message_id in reversed(chain)]


async def path_to(
    db: AsyncSession, *, session: m.ChatSession, message_id: uuid.UUID
) -> list[m.ChatMessage]:
    """Root to `message_id`, inclusive -- what a run answering at that point
    of the tree is given. Every message up to it for a linear session."""
    messages = await _all_messages(db, session=session)
    if not _has_tree(session) or session.current_leaf_id is None:
        return [msg for msg in messages if msg.id <= message_id]
    by_id = {msg.id: msg for msg in messages}
    chain = _ancestry({msg.id: msg.parent_id for msg in messages}, message_id)
    return [by_id[i] for i in reversed(chain)]


async def append_person_write(
    db: AsyncSession, *, session: m.ChatSession, message: m.ChatMessage
) -> None:
    """A send, an answer to a parked question, the refusal of a send, a
    follow-up or an oc8 notice: appended to the leaf, and the new leaf. Adds
    and flushes `message`, which may already have been flushed (without a
    parent) by a caller that needed its id early."""
    if not _has_tree(session):
        await _place(db, message=message, parent=None)
        return
    leaf = await lock_session(db, session=session)
    await _place(db, message=message, parent=leaf)
    await _set_leaf(db, session=session, leaf=message.id)


async def append_run_write(
    db: AsyncSession,
    *,
    session: m.ChatSession,
    message: m.ChatMessage,
    anchor_id: uuid.UUID | None,
) -> None:
    """A run's write (its answer, a carried-over steering turn), anchored on
    the user turn the run answers. While the leaf is the anchor or below it,
    appended to the leaf and the new leaf; otherwise (the person moved to
    another branch meanwhile) appended to the newest leaf below the anchor,
    and the leaf stays where the person is. No anchor, or one that no longer
    exists: a person's write. Adds and flushes `message`."""
    if not _has_tree(session):
        await _place(db, message=message, parent=None)
        return
    if anchor_id is None:
        await append_person_write(db, session=session, message=message)
        return
    leaf = await lock_session(db, session=session)
    parents = await _parents(db, session=session)
    parents.pop(message.id, None)
    if anchor_id not in parents:
        await append_person_write(db, session=session, message=message)
        return
    if leaf is None or anchor_id in _ancestry(parents, leaf):
        parent = leaf if leaf is not None else _newest_leaf(parents, anchor_id)
        await _place(db, message=message, parent=parent)
        await _set_leaf(db, session=session, leaf=message.id)
        return
    await _place(db, message=message, parent=_newest_leaf(parents, anchor_id))


async def add_regenerated_reply(
    db: AsyncSession,
    *,
    session: m.ChatSession,
    message: m.ChatMessage,
    first_variant_parent_id: uuid.UUID,
    anchor_id: uuid.UUID | None,
) -> None:
    """A regenerated answer: a sibling of the answer it regenerates (same
    parent). Like every run's write it becomes the leaf only while the leaf is
    its anchor (the user turn it answers) or below it -- a person who moved to
    another branch meanwhile stays there. A parent that is gone meanwhile
    falls back to `append_run_write`. Adds and flushes `message`."""
    if not _has_tree(session):
        await _place(db, message=message, parent=None)
        return
    leaf = await lock_session(db, session=session)
    parents = await _parents(db, session=session)
    parents.pop(message.id, None)
    if first_variant_parent_id not in parents:
        await append_run_write(db, session=session, message=message, anchor_id=anchor_id)
        return
    await _place(db, message=message, parent=first_variant_parent_id)
    if leaf is None or anchor_id is None or anchor_id in _ancestry(parents, leaf):
        await _set_leaf(db, session=session, leaf=message.id)


async def add_edited_message(
    db: AsyncSession,
    *,
    session: m.ChatSession,
    message: m.ChatMessage,
    original: m.ChatMessage,
) -> None:
    """An edited user turn: a sibling of the turn it rewrites (same parent),
    and the new leaf -- the person goes on from what they just wrote, while
    the old branch stays as it was. Adds and flushes `message`."""
    if not _has_tree(session):
        await _place(db, message=message, parent=None)
        return
    await lock_session(db, session=session)
    await _place(db, message=message, parent=original.parent_id)
    await _set_leaf(db, session=session, leaf=message.id)


async def switch_leaf(
    db: AsyncSession, *, session: m.ChatSession, message_id: uuid.UUID
) -> uuid.UUID:
    """Move the person to the branch through `message_id`: the leaf becomes
    the newest message below it. Announced like every leaf change; returns
    the new leaf. The caller checks that `message_id` is in this session."""
    await lock_session(db, session=session)
    leaf = _newest_leaf(await _parents(db, session=session), message_id)
    await _set_leaf(db, session=session, leaf=leaf)
    return leaf


async def newest_leaf_below(
    db: AsyncSession, *, session: m.ChatSession, message_id: uuid.UUID
) -> uuid.UUID:
    """The newest message without children below `message_id` (itself when it
    has none): where a branch picked at `message_id` continues."""
    return _newest_leaf(await _parents(db, session=session), message_id)


async def remove_message(
    db: AsyncSession, *, session: m.ChatSession, message_id: uuid.UUID
) -> None:
    """Delete one message without breaking the tree: its children move up to
    its parent, and when it was the leaf, the leaf moves to its parent."""
    if not _has_tree(session):
        await db.execute(
            delete(m.ChatMessage).where(
                m.ChatMessage.tenant_id == session.tenant_id,
                m.ChatMessage.session_id == session.id,
                m.ChatMessage.id == message_id,
            )
        )
        return
    leaf = await lock_session(db, session=session)
    in_session = (
        m.ChatMessage.tenant_id == session.tenant_id,
        m.ChatMessage.session_id == session.id,
    )
    parent = (
        await db.execute(
            select(m.ChatMessage.parent_id).where(*in_session, m.ChatMessage.id == message_id)
        )
    ).scalar_one_or_none()
    await db.execute(
        update(m.ChatMessage)
        .where(*in_session, m.ChatMessage.parent_id == message_id)
        .values(parent_id=parent)
        .execution_options(synchronize_session="fetch")
    )
    await db.execute(delete(m.ChatMessage).where(*in_session, m.ChatMessage.id == message_id))
    if leaf == message_id:
        await _set_leaf(db, session=session, leaf=parent)
