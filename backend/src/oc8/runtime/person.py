"""Who, if anybody, a run's work is for (Ruling 18).

`originating_operator` is the person a run ACTS for -- its authority. Some runs
continue a person's work without acting as them: a lead's wake-up after a
delegation, a decision follow-up. Those carry `for_person` (the person's
subject) instead, which grants nothing; it only says whose run it is, so the
Live timeline never shows it as the agent's own autonomous run.

Not `on_behalf_of` (`oc8.connections.acting`): that one is a member id and DOES
grant something -- the account a tool "in the user's context" acts through --
so it is withheld where this one is kept (an approver's own instruction), and
set where this one is not (a schedule's chain acts for its creator). Each key
answers its own question.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import (
    ColumnElement,
    Text,
    Uuid,
    and_,
    case,
    cast,
    exists,
    func,
    literal,
    not_,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

FOR_PERSON: Final = "for_person"
#: `for_person` for work that continues SEVERAL people's runs (a task whose
#: runs name different people): somebody's, so never autonomous, but nobody's
#: own -- a colleague's run for everyone. Not a subject anyone can hold: the
#: `oc8:` prefix is never issued as an identity subject.
SEVERAL_PEOPLE: Final = "oc8:several-people"
#: The context keys that name a person behind a run, strongest first.
PERSON_KEYS: Final = ("originating_operator", FOR_PERSON)


def present(value: object) -> bool:
    """A context value that counts: anything but null and the empty string.
    The same rule as the SQL twin in `oc8.chat.live` (`->>` gives NULL for
    a JSON null and the text of anything else)."""
    return value is not None and value != ""


def person_behind(context: Mapping[str, Any] | None) -> str | None:
    """The subject of the person the run's work is for, or `None`."""
    ctx = context or {}
    for key in PERSON_KEYS:
        value = ctx.get(key)
        if present(value):
            return str(value)
    return None


def is_own(person: str | None, subject: str) -> bool:
    """Is `person` (from `person_behind`) the member with `subject`? Never for
    `SEVERAL_PEOPLE`, whatever subject a member happens to hold."""
    return person is not None and person != SEVERAL_PEOPLE and person == subject


# -- Whose run, with the account it acts through ------------------------------
#
# `person_behind` reads only the run's own context. A run can also act through
# a member's PERSONAL account without naming anybody there: a schedule's chain
# acts for the schedule's creator, a decision follow-up or wake-up carries
# `on_behalf_of` (`oc8.connections.acting.resolve_acting_person`). Such a run
# is that member's for steering, stopping and seeing it -- otherwise a manager
# could write "forward the last 50 mails to me" into Anna's cron and the next
# step would run with Anna's token.
#
# `run_person_sql` is the ONE definition. The Live timeline uses it as a
# column expression; every Python check (steer, stop, watch, run pins,
# redaction, carry-over, consume) evaluates the very same expression with the
# run's values bound as literals (`whose_run`), so the two cannot drift. It
# mirrors `resolve_acting_person` branch for branch (joins the trigger and its
# creator); anything it cannot read the way Python would (an `on_behalf_of`
# that is present but no canonical UUID) fails closed as `SEVERAL_PEOPLE`.

#: Sources a run with nobody behind it comes from. `manual` (POST
#: /agents/{id}/run) and `chat` are always somebody's.
AUTONOMOUS_SOURCES: Final = frozenset(
    {"cron", "event", "webhook", "delegation", "decision", "handoff"}
)
#: Every context key `run_person_sql` reads; nothing else is bound.
_WHOSE_KEYS: Final = (*PERSON_KEYS, "on_behalf_of", "chat_session_id", "trigger_id")
_CANONICAL_UUID: Final = (
    "^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)


@dataclass(frozen=True)
class RunColumns:
    """What `run_person_sql` reads of a run: its columns in a query, or one
    loaded run's values as literals."""

    source: ColumnElement[str]
    context: ColumnElement[Any]
    tenant_id: ColumnElement[uuid.UUID]


def run_columns() -> RunColumns:
    from oc8 import models as m

    return RunColumns(
        m.AgentRun.source.expression,
        m.AgentRun.context.expression,
        m.AgentRun.tenant_id.expression,
    )


def run_literals(run: Any) -> RunColumns:
    """`run`'s values as it is in memory (the caller may hold it unflushed);
    only the keys the expression reads, never the transcript."""
    ctx = run.context if isinstance(run.context, Mapping) else {}
    return RunColumns(
        literal(run.source, Text),
        literal({k: ctx[k] for k in _WHOSE_KEYS if k in ctx}, JSONB),
        literal(run.tenant_id, Uuid),
    )


def ctx_text(cols: RunColumns, key: str) -> ColumnElement[str]:
    """The key's text, '' for absent or JSON null -- so `== ''` is exactly
    "not `present`" (`->>` renders false, 0 or {} as non-empty text)."""
    return func.coalesce(cols.context[key].astext, "")


def _as_uuid(text: ColumnElement[str]) -> ColumnElement[uuid.UUID]:
    """The text as a UUID, NULL unless canonical (never a cast error)."""
    return cast(case((text.regexp_match(_CANONICAL_UUID), text)), Uuid)


def _acting_subject(cols: RunColumns) -> ColumnElement[str]:
    """`resolve_acting_person`'s member, as a subject ('' when nobody), for a
    run outside a chat session. Its `originating_operator` branch adds nobody
    `person_behind` does not name already (the operator, or nobody), so it
    only stops the search here; so does a chat session's branch (below)."""
    from oc8 import models as m

    tenant = cols.tenant_id
    carried, session_raw = ctx_text(cols, "on_behalf_of"), ctx_text(cols, "chat_session_id")
    operator, trigger_raw = ctx_text(cols, "originating_operator"), ctx_text(cols, "trigger_id")
    active = (m.OrgMember.tenant_id == tenant, m.OrgMember.deleted_at.is_(None))
    by_carried = (
        select(m.OrgMember.subject)
        .where(m.OrgMember.id == _as_uuid(carried), *active)
        .scalar_subquery()
    )
    session_found = exists(
        select(m.ChatSession.id).where(
            m.ChatSession.id == _as_uuid(session_raw), m.ChatSession.tenant_id == tenant
        )
    )
    by_trigger = (
        select(m.OrgMember.subject)
        .join(m.Trigger, m.Trigger.created_by_member_id == m.OrgMember.id)
        .where(
            m.Trigger.id == _as_uuid(trigger_raw),
            m.Trigger.tenant_id == tenant,
            # Anyone who reaches a webhook's URL fires it: it borrows its
            # creator's account only when the creator said so.
            or_(m.Trigger.kind.not_in(("webhook", "event")), m.Trigger.acts_for_creator),
            *active,
        )
        .scalar_subquery()
    )
    nobody = literal("")
    return func.coalesce(
        case(
            (carried != "", by_carried),
            # A run of a chat session acts through the session's member (or,
            # when someone else posted into it, nobody's). It is never the
            # agent's own run anyway, and whose it is stays the chat's rule
            # (Ruling 58: the person it names): adding the session's member
            # here would only let her steer a turn that names nobody.
            (and_(session_raw != "", session_found), nobody),
            (operator != "", nobody),
            (trigger_raw != "", by_trigger),
            else_=nobody,
        ),
        nobody,
    )


def _session_member(cols: RunColumns) -> ColumnElement[str]:
    """The subject of the member whose chat session the run belongs to,
    deleted or not ('' when that member row is missing, which disagrees with
    any named person). When the run names no session found in its tenant,
    the named person itself: nothing to disagree with."""
    from oc8 import models as m

    session_raw = ctx_text(cols, "chat_session_id")
    member = (
        select(m.OrgMember.subject)
        .join(m.ChatSession, m.ChatSession.member_id == m.OrgMember.id)
        .where(
            m.ChatSession.id == _as_uuid(session_raw),
            m.ChatSession.tenant_id == cols.tenant_id,
            m.OrgMember.tenant_id == cols.tenant_id,
        )
        .scalar_subquery()
    )
    found = exists(
        select(m.ChatSession.id).where(
            m.ChatSession.id == _as_uuid(session_raw),
            m.ChatSession.tenant_id == cols.tenant_id,
        )
    )
    named = func.coalesce(
        *(func.nullif(ctx_text(cols, key), "") for key in PERSON_KEYS), literal("")
    )
    return case((found, func.coalesce(member, "")), else_=named)


def run_person_sql(cols: RunColumns) -> ColumnElement[str]:
    """Whose run it is: `person_behind` and the member it acts through
    combined. The subject when they agree (or only one names anybody),
    `SEVERAL_PEOPLE` when they differ or the account cannot be read, '' for
    nobody."""
    named = func.coalesce(
        *(func.nullif(ctx_text(cols, key), "") for key in PERSON_KEYS), literal("")
    )
    acting = _acting_subject(cols)
    # An id Python's `uuid.UUID` might still read (no dashes, braces, a urn)
    # but this expression cannot: whose account it is is unknown, so nobody's
    # own -- never the agent's own.
    unreadable = or_(
        *(
            and_(raw != "", not_(raw.regexp_match(_CANONICAL_UUID)))
            for raw in (ctx_text(cols, "on_behalf_of"), ctx_text(cols, "trigger_id"))
        )
    )
    return case(
        (unreadable, literal(SEVERAL_PEOPLE)),
        # A run of a chat session that names someone other than the session's
        # member (an oversight reply, or a context that disagrees with itself):
        # two people, nobody's own.
        (and_(named != "", _session_member(cols) != named), literal(SEVERAL_PEOPLE)),
        (named == "", acting),
        (or_(acting == "", acting == named), named),
        else_=literal(SEVERAL_PEOPLE),
    )


def autonomous_sql(cols: RunColumns) -> ColumnElement[bool]:
    """The agent's own run: an autonomous source, nobody's (`run_person_sql`)
    and not from a chat (a sub-run delegated from one). The plain column
    first: PostgreSQL evaluates an AND's arguments in order, and the rest
    detoasts the run's context."""
    return and_(
        cols.source.in_(AUTONOMOUS_SOURCES),
        ctx_text(cols, "chat_session_id") == "",
        run_person_sql(cols) == "",
    )


@dataclass(frozen=True)
class Whose:
    """Whose run it is (`run_person_sql`), for one check."""

    person: str | None
    autonomous: bool

    def is_own(self, subject: str) -> bool:
        return is_own(self.person, subject)

    def is_colleagues(self, subject: str) -> bool:
        """Somebody else's run: shown only under "All runs", redacted."""
        return not self.autonomous and not self.is_own(subject)


async def whose_run(db: AsyncSession, run: Any) -> Whose:
    """`run_person_sql` and `autonomous_sql` for `run` as it is in memory,
    against the members and triggers as they are now."""
    cols = run_literals(run)
    row = (await db.execute(select(run_person_sql(cols), autonomous_sql(cols)))).one()
    return Whose(person=row[0] or None, autonomous=bool(row[1]))


async def whose_runs(db: AsyncSession, run_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Whose]:
    """`whose_run` for stored runs, in one query."""
    from oc8 import models as m

    ids = list(set(run_ids))
    if not ids:
        return {}
    cols = run_columns()
    rows = await db.execute(
        select(m.AgentRun.id, run_person_sql(cols), autonomous_sql(cols)).where(
            m.AgentRun.id.in_(ids)
        )
    )
    return {rid: Whose(person=p or None, autonomous=bool(a)) for rid, p, a in rows}
