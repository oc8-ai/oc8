"""What a sync may conclude from a connector's listing, and the tick that acts on it.

Two halves, deliberately split by what they are allowed to do.

**Observation** runs inside `run_source_sync` -- the single connector-agnostic
funnel, the only code that owns the cursor and the only code that knows whether
this sync actually worked. It stamps facts on rows and **never deletes**.

**Decision** runs in the sweep, on the worker's housekeeping timer. It performs
no network IO and calls no connector: it acts only on facts a sync already wrote.

**Core's permitted conclusions per attestation live here and nowhere else.** §3
of the design puts them in this docstring on purpose. The seam hands core a
`frozenset[str]` and one `Attestation` member -- never a `maxFiles`, a bucket or
a folder id -- and this is the one place that says what they mean:

* `AUTHORITATIVE` -- the listing is everything the source now holds, enumerated
  to the end of its pagination. A document this source previously delivered
  whose URI is absent is a **candidate**, not a corpse. Only this member
  forgives, only this member adopts an orphan and only this member advances
  `last_attested_sync_at`, because all three are claims about PRESENCE and it is
  the only member that can make one.
* `REMOVALS` -- exactly the URIs in `removed_uris` are candidates. Absence from
  the listing means nothing: it never claimed to have seen everything. So it
  never forgives an earlier mark and never attributes an unattributed row.
* `NONE` -- conclude nothing. A connector with no `attest_listing` at all, an
  explicit `NONE`, an exception and a timeout are one outcome, and it is the
  outcome that costs a customer nothing.

**Two facts must agree before a chunk dies**, and they are one SQL comparison:
`data_source.last_attested_sync_at >= kb_chunk.missing_since +
knowledge_missing_confirm_hours`. Fact A is that an attested listing once did
not contain the document; fact B is that a **second, later** attested sync of
the same source, at least the confirm window afterwards, also did not. The
separation is the point -- without it two syncs a minute apart during a
one-minute permissions blip count as two observations that agree. Say plainly
what this is: **the same witness twice, a day apart, not two independent
witnesses.** It defends against transient failure and not at all against
systematic failure, which is what the refusals below are for.

**The refusals all default to keeping the content.** An expired OAuth token, a
revoked bucket policy, a rotated credential and a folder id that no longer
resolves all look exactly like "the customer deleted everything"; a listing
built with a slightly different URI shape looks exactly like "the customer
replaced everything". Each refusal parks the source in `reconcile_state='held'`
with a note in words, marks nothing, and does not advance fact B. The customer
who really did empty a source has `DELETE /knowledge/sources/{id}`, and the note
says so -- a hold with no remedy named sends an operator hunting a bug in the
sweep.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, Text, and_, any_, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.audit.chain import append_event
from oc8.config import get_settings
from oc8.db.session import tenant_session
from oc8.knowledge.connectors.base import Attestation, AuthContext, SourceListing
from oc8.knowledge.tombstone import (
    CATEGORY,
    RECONCILE_HELD,
    SCOPE_CHANGED,
    SOURCE_ABSENT,
    SUPERSEDED,
    SUPERSEDED_REDUCED,
    reduce_document,
    tombstone_document,
)
from oc8.triggers.scheduler import list_active_tenant_ids

logger = logging.getLogger(__name__)

#: How long core waits for a connector to say what it holds. The listing is one
#: API page per 1000 objects and carries no content, but it is still a call to
#: somebody else's server inside a sync, and a hung remote must not hold a
#: database transaction open. Timing out means `NONE`, which costs nothing.
_ATTEST_TIMEOUT = 30.0

#: How many documents one tenant may contribute to a single tick, in each phase.
#: The sweep shares the worker's housekeeping timer with the run loop, so a
#: backlog of ten thousand tombstones must not turn one tick into an hour. The
#: backlog drains over the following ticks; the columns keep the work list exact.
_BATCH_DOCS = 100

#: The endpoint a held source's operator actually needs. Named in every hold
#: note because "held" with no remedy reads as a bug in the sweep (residual risk
#: 4): the customer who genuinely emptied a source is supposed to use this.
_SOURCE_ENDPOINT = "DELETE /knowledge/sources/{id}"


@dataclass
class KnowledgeReconcileReport:
    """What one tick did. Returned rather than only logged, so the CLI one-shot
    can print it and the tests can assert on it.

    `documents_reduced` and `superseded_reduced` are DISJOINT counts of
    documents: the first is content destroyed because it vanished upstream, each
    with its own ledger entry; the second is a stale generation destroyed after a
    re-ingest replaced it, all of them under one aggregate entry per tenant per
    tick.

    There are no `refused_*` counters. The design lists three, but the refusals
    happen in `observe_listing`, which runs inside a sync and never sees this
    object -- a counter that is structurally always zero is worse than no
    counter. A refusal surfaces where an operator will actually meet it:
    `data_source.reconcile_state`, `reconcile_note`, one `knowledge.reconcile_held`
    chain row, and `job.stats["reconcile"]` on the sync that refused.
    """

    tenants: int = 0
    documents_tombstoned: int = 0
    documents_reduced: int = 0
    superseded_reduced: int = 0
    chunks: int = 0
    failures: int = 0


def _uri_is_one_of(uris: Collection[str]) -> ColumnElement[bool]:
    """`source_uri = ANY(:uris)` -- one bound array, not one parameter per URI.

    Amendment A1 made listings unbounded, so `present_uris` for a 5000-object
    bucket really does carry 5000 entries; an expanded IN list would compile a
    5000-parameter statement on every sync of every source.
    """
    return m.KbChunk.source_uri == any_(literal(sorted(uris), ARRAY(Text)))


# --------------------------------------------------------------- observation


async def _hold(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    data_source: m.DataSource,
    kb_id: uuid.UUID,
    note: str,
    facts: dict[str, Any],
) -> None:
    """Park the source and say why, in words, in three places an operator looks.

    The chain row is written only when the hold or its wording actually CHANGES.
    A source held by the vanish guard stays held until a human acts (residual
    risk 4), and appending one row per sync for months would grow an append-only
    table as a side effect of ordinary operation -- the same objection §5.2
    raises against a ledger entry per superseded document.
    """
    changed = data_source.reconcile_state != "held" or data_source.reconcile_note != note
    data_source.reconcile_state = "held"
    data_source.reconcile_note = note
    if not changed:
        return
    await append_event(
        db,
        tenant_id=tenant_id,
        actor_type="system",
        actor_id=None,
        category=CATEGORY,
        action=RECONCILE_HELD,
        resource={
            "data_source_id": str(data_source.id),
            "kb_id": str(kb_id),
            "connector_type": data_source.connector_type,
            **facts,
        },
        # A refusal, not a permission: core declined to act on a listing it could
        # not believe. Nothing was marked and nothing was destroyed.
        decision="deny",
        reason=note,
    )
    await db.flush()


async def observe_listing(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    data_source: m.DataSource,
    kb_id: uuid.UUID,
    connector: Any,
    auth: AuthContext | None,
    fatal: str | None,
    now: dt.datetime,
) -> str:
    """Stamp what an attested listing says about this source. Never deletes.

    Returns one of `"disabled"`, `"failed_sync"`, `"no_attestation"`, `"none"`,
    `"held_empty"`, `"held_disjoint"`, `"held_fraction"`, `"observed"` --
    recorded on `job.stats["reconcile"]` so an operator can tell "nothing to
    delete" from "refused to delete" from "cannot delete". flush-only; the caller
    owns the commit.
    """
    settings = get_settings()
    # The flag gates observation as well as the sweep. Gating only the sweep
    # would leave a default deployment writing marks, held state and chain rows
    # into an append-only ledger while nothing could ever act on them.
    if not settings.knowledge_reconcile_enabled:
        return "disabled"

    # Gated on the CONNECTOR-level fatal, not on `job.status`. A single
    # unsupported content type in a Drive folder makes the job `partial`, which
    # says nothing about listing completeness and would otherwise disable
    # propagation for that source permanently and invisibly.
    if fatal is not None:
        return "failed_sync"

    # Duck-typed rather than isinstance'd against `AttestingConnector`, exactly
    # as `evidence/sweep.py` resolves the optional evidence half of the runtime
    # seam: a plugin loaded from outside this package only has to answer the
    # name, not import core's type to prove it.
    attest = getattr(connector, "attest_listing", None)
    if not callable(attest):
        return "no_attestation"
    try:
        # A hung remote must not hold a database transaction open, and timing
        # out means NONE, which costs a customer nothing.
        async with asyncio.timeout(_ATTEST_TIMEOUT):
            listing = await attest(data_source.config, auth)
        if not isinstance(listing, SourceListing):
            raise TypeError(
                f"attest_listing returned {type(listing).__name__}, not a SourceListing"
            )
    except Exception:
        # An API error, a revoked grant, a timeout, a plugin that answers the
        # name with nonsense. A connector RAISES rather than attesting an empty
        # set precisely so this branch exists: a revoked grant answering
        # AUTHORITATIVE with no URIs is the wire shape of "the customer deleted
        # everything".
        logger.warning(
            "knowledge: source %s could not attest its listing", data_source.id, exc_info=True
        )
        return "none"

    if listing.attestation is Attestation.NONE:
        logger.debug(
            "knowledge: source %s attests nothing (%s)", data_source.id, listing.reason or "-"
        )
        return "none"

    if listing.attestation is Attestation.REMOVALS:
        # Marks exactly what it names and nothing else. It does not advance
        # `last_attested_sync_at`: that column carries fact B, and fact B is "a
        # later listing that enumerated EVERYTHING and still did not contain
        # it", which a REMOVALS listing has not claimed and cannot.
        await _mark(
            db, tenant_id=tenant_id, data_source=data_source, uris=listing.removed_uris, now=now
        )
        return "observed"

    return await _observe_authoritative(
        db,
        tenant_id=tenant_id,
        data_source=data_source,
        kb_id=kb_id,
        present=listing.present_uris,
        now=now,
    )


async def _observe_authoritative(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    data_source: m.DataSource,
    kb_id: uuid.UUID,
    present: frozenset[str],
    now: dt.datetime,
) -> str:
    """Adopt orphans, read the baseline, apply the three refusals, mark and forgive.

    `kb_id` reaches exactly one of those: orphan adoption, which attributes rows
    in the base this sync is writing into. Everything else is source-wide,
    because the attestation is a claim about the source and so is every column
    that carries its consequences.
    """
    if present:
        # An attested listing is precisely the evidence that authorises the
        # attribution: this source says it holds that URI. Without it the slice
        # is inert on every existing corpus -- the 0045 backfill can only reach
        # `upload://` URIs, and the hash cursor means an unchanged document is
        # never re-yielded, so a pre-0045 Drive chunk would never acquire a
        # source id on any future sync either. Rows already attributed to another
        # source are never touched.
        await db.execute(
            update(m.KbChunk)
            .where(
                m.KbChunk.tenant_id == tenant_id,
                m.KbChunk.kb_id == kb_id,
                m.KbChunk.data_source_id.is_(None),
                m.KbChunk.deleted_at.is_(None),
                _uri_is_one_of(present),
            )
            .values(data_source_id=data_source.id)
        )

    # The baseline, read AFTER adoption so a just-attributed row counts. One
    # query carries both halves: which documents this source has live ANYWHERE,
    # and which of them are not yet marked.
    #
    # Deliberately NOT scoped to `kb_id`, and this is the fix for a measured
    # data-loss path. The attestation is a claim about the SOURCE -- "this is
    # everything it now holds" -- and every consequence of believing it is
    # source-wide: `_mark` and the forgive UPDATE carry no kb predicate, and
    # `last_attested_sync_at`, which is fact B itself, is a column on
    # `data_source` that the sweep joins with no kb predicate either. Comparing
    # that claim against one base's slice meant a listing refused as implausible
    # against the base it belongs to was silently ACCEPTED -- and became witness
    # #2 -- when the same source synced a base where it had no live chunks yet.
    # A Drive folder id that stopped resolving answers 200 with `files: []`, so
    # nothing raises and no hold is taken: measured, a document died on one real
    # observation plus a listing that was never compared against anything. The
    # fact that licenses a deletion and the refusals that guard it now have the
    # same scope.
    rows = (
        await db.execute(
            select(m.KbChunk.source_uri, func.bool_or(m.KbChunk.missing_since.is_(None)))
            .where(
                m.KbChunk.tenant_id == tenant_id,
                m.KbChunk.data_source_id == data_source.id,
                m.KbChunk.deleted_at.is_(None),
            )
            .group_by(m.KbChunk.source_uri)
        )
    ).all()
    known = {uri for uri, _ in rows}
    unmarked = {uri for uri, fresh in rows if fresh}

    missing = known - present
    # Over NEWLY missing documents, not all missing ones. With the cumulative
    # form the missing set only ever grows -- a document leaves the baseline only
    # when the sweep kills it, and the sweep is off by default -- so a source
    # that loses 20% now and 40% next month would latch to `held` and never clear.
    missing_new = missing & unmarked
    settings = get_settings()

    if known and not present:
        note = (
            f"the connector attested an EMPTY listing while this source still has "
            f"{len(known)} live document(s). An expired credential, a revoked policy "
            f"and an emptied folder are the same wire response, so nothing was marked. "
            f"If the source really is empty, use {_SOURCE_ENDPOINT}."
        )
        await _hold(
            db,
            tenant_id=tenant_id,
            data_source=data_source,
            kb_id=kb_id,
            note=note,
            facts={"refusal": "empty", "known": len(known), "listed": 0},
        )
        logger.warning("knowledge: held source %s -- %s", data_source.id, note)
        return "held_empty"

    if known and present and not (known & present):
        # URI-construction drift: the listing is perfectly valid and describes a
        # different namespace (bare S3 keys against `s3://bucket/key` chunks).
        # One check turns a plugin bug into a log line instead of a wiped corpus.
        note = (
            f"the attested listing ({len(present)} URI(s)) shares no URI with the "
            f"{len(known)} document(s) this source ingested. That is a connector "
            f"building URIs differently from the ones it fetches, not a deletion."
        )
        await _hold(
            db,
            tenant_id=tenant_id,
            data_source=data_source,
            kb_id=kb_id,
            note=note,
            facts={"refusal": "disjoint", "known": len(known), "listed": len(present)},
        )
        logger.warning("knowledge: held source %s -- %s", data_source.id, note)
        return "held_disjoint"

    if (
        known
        and len(missing_new) > settings.knowledge_vanish_min_documents
        and len(missing_new) / len(known) > settings.knowledge_vanish_ratio_limit
    ):
        # The floor exists because ratios behave badly at small N: a two-document
        # source losing its second is not evidence of a credential failure.
        note = (
            f"{len(missing_new)} of {len(known)} document(s) vanished from a single "
            f"attested listing, over the "
            f"{settings.knowledge_vanish_ratio_limit:.0%} limit, so nothing was marked. "
            f"If the source really lost them, use {_SOURCE_ENDPOINT}."
        )
        await _hold(
            db,
            tenant_id=tenant_id,
            data_source=data_source,
            kb_id=kb_id,
            note=note,
            facts={
                "refusal": "fraction",
                "known": len(known),
                "listed": len(present),
                "missing_new": len(missing_new),
            },
        )
        logger.warning("knowledge: held source %s -- %s", data_source.id, note)
        return "held_fraction"

    await _mark(db, tenant_id=tenant_id, data_source=data_source, uris=missing, now=now)
    if present:
        # Forgive: a document that comes back is not dying. `missing_since IS NOT
        # NULL` is deliberate -- without it every tick rewrites every chunk row of
        # every present document, which is write amplification on the widest
        # table in the schema.
        await db.execute(
            update(m.KbChunk)
            .where(
                m.KbChunk.tenant_id == tenant_id,
                m.KbChunk.data_source_id == data_source.id,
                m.KbChunk.deleted_at.is_(None),
                m.KbChunk.missing_since.is_not(None),
                _uri_is_one_of(present),
            )
            .values(missing_since=None)
        )

    data_source.last_attested_sync_at = now
    # A hold clears automatically on the next attested listing that passes all
    # three refusals -- there is no manual reset, because a stuck `held` that
    # only a support ticket can clear is worse than the risk it guards.
    data_source.reconcile_state = "ok"
    data_source.reconcile_note = None
    await db.flush()
    return "observed"


async def _mark(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    data_source: m.DataSource,
    uris: Collection[str],
    now: dt.datetime,
) -> None:
    """Record the FIRST observation that a document was absent, and only the first.

    `missing_since IS NULL` in the WHERE is what makes fact B reachable at all:
    refreshing the timestamp on every later sync would move the goalpost forward
    by exactly as much as the clock, so the confirm window would never elapse.
    """
    if not uris:
        return
    await db.execute(
        update(m.KbChunk)
        .where(
            m.KbChunk.tenant_id == tenant_id,
            m.KbChunk.data_source_id == data_source.id,
            m.KbChunk.deleted_at.is_(None),
            m.KbChunk.missing_since.is_(None),
            _uri_is_one_of(uris),
        )
        .values(missing_since=now)
    )
    await db.flush()


# --------------------------------------------------------------- the sweep


def _condemned_where(tenant_id: uuid.UUID, *, confirm: dt.timedelta) -> list[ColumnElement[bool]]:
    """The four things that must ALL still be true for a document to die.

    One definition, used twice on purpose: once to build the tick's work list and
    once again inside the transaction that actually writes, exactly as
    `evidence/sweep.py` re-reads its state column before acting on it. The work
    list is read up to a hundred documents earlier, and everything in it can be
    retracted in between -- measured: a sync committing in that window FORGAVE
    the document (`missing_since` back to NULL) and parked the source `held`
    because core had refused its listing, and the sweep tombstoned it anyway on a
    fact that had already been withdrawn.
    """
    return [
        m.KbChunk.tenant_id == tenant_id,
        # Unknown is not the same as done: a row written before 0045 carries no
        # provenance, and it is outside the mechanism entirely rather than absent
        # from a source nobody can name.
        m.KbChunk.data_source_id.is_not(None),
        m.KbChunk.deleted_at.is_(None),
        # Fact A.
        m.KbChunk.missing_since.is_not(None),
        # Fact B, and the confirm window, in one comparison.
        m.DataSource.last_attested_sync_at >= m.KbChunk.missing_since + confirm,
        m.DataSource.reconcile_state == "ok",
        m.DataSource.deleted_at.is_(None),
    ]


def _source_join() -> Any:
    return and_(
        m.DataSource.id == m.KbChunk.data_source_id,
        m.DataSource.tenant_id == m.KbChunk.tenant_id,
    )


async def _still_condemned(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kb_id: uuid.UUID,
    data_source_id: uuid.UUID,
    source_uri: str,
    confirm: dt.timedelta,
) -> bool:
    """Does this one document still satisfy every predicate that selected it?

    Read inside the writing transaction. A `False` here is not a failure and is
    not counted: it means the world moved on between the work list and now, and
    the right thing to do about a document that came back, or a source core has
    stopped believing, is nothing.
    """
    found = (
        await db.execute(
            select(m.KbChunk.id)
            .join(m.DataSource, _source_join())
            .where(
                *_condemned_where(tenant_id, confirm=confirm),
                m.KbChunk.kb_id == kb_id,
                m.KbChunk.data_source_id == data_source_id,
                m.KbChunk.source_uri == source_uri,
            )
            .limit(1)
        )
    ).first()
    return found is not None


async def _sweep_tenant(
    tenant_id: uuid.UUID, *, now: dt.datetime, report: KnowledgeReconcileReport
) -> None:
    settings = get_settings()
    confirm = dt.timedelta(hours=max(settings.knowledge_missing_confirm_hours, 0))
    grace = dt.timedelta(hours=max(settings.knowledge_reduce_after_hours, 0))

    # Both work lists are read once, as plain ids, in a transaction that writes
    # nothing. Every unit of work below then opens its OWN transaction: the
    # tenant binding is transaction-local, so a session that commits mid-loop
    # goes tenant-blind for everything after it, and RLS answers a blind session
    # with silence rather than an error. That is the bug that nearly shipped in
    # the evidence sweep (tests/evidence/test_sweep.py:415) -- a sweep that did
    # exactly one document per tenant per tick and reported success.
    async with tenant_session(tenant_id) as db:
        condemned = list(
            (
                await db.execute(
                    select(m.KbChunk.kb_id, m.KbChunk.data_source_id, m.KbChunk.source_uri)
                    .join(m.DataSource, _source_join())
                    .where(*_condemned_where(tenant_id, confirm=confirm))
                    .group_by(m.KbChunk.kb_id, m.KbChunk.data_source_id, m.KbChunk.source_uri)
                    .order_by(func.min(m.KbChunk.missing_since))
                    .limit(_BATCH_DOCS)
                )
            ).all()
        )
        expired = list(
            (
                await db.execute(
                    select(
                        m.KbChunk.kb_id,
                        m.KbChunk.data_source_id,
                        m.KbChunk.source_uri,
                        m.KbChunk.deleted_reason,
                    )
                    .join(m.DataSource, _source_join(), isouter=True)
                    .where(
                        m.KbChunk.tenant_id == tenant_id,
                        m.KbChunk.data_source_id.is_not(None),
                        m.KbChunk.deleted_at.is_not(None),
                        m.KbChunk.reduced_at.is_(None),
                        # A source core has stopped believing does not get its
                        # inferred absences DESTROYED while a human is being
                        # asked to look at it -- that is rule 1's dangerous half
                        # arriving a week late. The tombstone stays (the document
                        # is already out of retrieval) and stays restorable until
                        # the hold clears. A `superseded` generation is exempt:
                        # it was replaced by a re-ingest, which has nothing to do
                        # with whether an attested listing could be believed.
                        or_(
                            m.KbChunk.deleted_reason == SUPERSEDED,
                            and_(
                                m.DataSource.reconcile_state == "ok",
                                m.DataSource.deleted_at.is_(None),
                            ),
                        ),
                        # An operator's deletion was reduced in the transaction
                        # that tombstoned it; only an INFERENCE waits out a grace
                        # window, which is what makes it restorable.
                        # `scope_changed` reaches here only unreduced -- an
                        # emptied scope, kept restorable for this same window.
                        m.KbChunk.deleted_reason.in_([SOURCE_ABSENT, SUPERSEDED, SCOPE_CHANGED]),
                        m.KbChunk.deleted_at < now - grace,
                    )
                    .group_by(
                        m.KbChunk.kb_id,
                        m.KbChunk.data_source_id,
                        m.KbChunk.source_uri,
                        m.KbChunk.deleted_reason,
                    )
                    .order_by(func.min(m.KbChunk.deleted_at))
                    .limit(_BATCH_DOCS)
                )
            ).all()
        )

    for kb_id, ds_id, uri in condemned:
        try:
            async with tenant_session(tenant_id) as db:
                if not await _still_condemned(
                    db,
                    tenant_id=tenant_id,
                    kb_id=kb_id,
                    data_source_id=ds_id,
                    source_uri=uri,
                    confirm=confirm,
                ):
                    # Retracted since the work list was read. Not a failure, not
                    # counted, and nothing written.
                    continue
                removal = await tombstone_document(
                    db,
                    tenant_id=tenant_id,
                    kb_id=kb_id,
                    data_source_id=ds_id,
                    source_uri=uri,
                    reason=SOURCE_ABSENT,
                    reduce_now=False,
                    now=now,
                )
        except Exception:
            # One document must not cost every other document. The row keeps its
            # mark, so the next tick tries again.
            report.failures += 1
            logger.warning("knowledge: could not tombstone %s", uri, exc_info=True)
            continue
        if removal is None:
            continue  # another worker got there first
        report.documents_tombstoned += 1
        report.chunks += removal.chunks

    superseded_docs = 0
    superseded_chunks = 0
    for kb_id, ds_id, uri, reason in expired:
        try:
            async with tenant_session(tenant_id) as db:
                # The ledger entry and the destruction are inside this one
                # transaction and commit together on the way out. Do NOT commit
                # between them: the entry would be durable, the tenant GUC gone,
                # and the UPDATE would match nothing under RLS -- leaving a
                # permanent, unrepairable claim that a document was destroyed
                # while its text sat there.
                removal = await reduce_document(
                    db,
                    tenant_id=tenant_id,
                    kb_id=kb_id,
                    data_source_id=ds_id,
                    source_uri=uri,
                    # The work list's own two predicates, handed on rather than
                    # left implicit. Without them the function re-selected every
                    # unreduced generation of the URI and destroyed a document
                    # still days inside its grace window, under whichever
                    # `deleted_reason` the database returned first.
                    reason=reason,
                    deleted_before=now - grace,
                    now=now,
                )
        except Exception:
            report.failures += 1
            logger.warning("knowledge: could not reduce %s", uri, exc_info=True)
            continue
        if removal is None:
            # The claim, re-read inside the writing transaction: no unreduced
            # tombstone left means somebody else got there first, and this tick
            # wrote no entry claiming a destruction it did not perform.
            continue
        report.chunks += removal.chunks
        if reason == SUPERSEDED:
            superseded_docs += 1
            superseded_chunks += removal.chunks
        else:
            report.documents_reduced += 1

    if superseded_docs:
        try:
            async with tenant_session(tenant_id) as db:
                # ONE aggregate entry per tenant per tick, with no digests. A
                # superseded chunk's content is a stale copy of upstream, not a
                # record of an action -- the IngestionJob already records the
                # re-ingest -- so per-document rows would grow an append-only
                # table without bound as a side effect of ordinary operation,
                # each one taking the tenant's audit lock.
                await append_event(
                    db,
                    tenant_id=tenant_id,
                    actor_type="system",
                    actor_id=None,
                    category=CATEGORY,
                    action=SUPERSEDED_REDUCED,
                    resource={"documents": superseded_docs, "chunks": superseded_chunks},
                    decision="allow",
                    reason="superseded generations passed their grace window",
                )
        except Exception:
            report.failures += 1
            logger.warning("knowledge: could not record the superseded aggregate", exc_info=True)
        else:
            report.superseded_reduced += superseded_docs


async def sweep_knowledge_deletions(*, now: dt.datetime | None = None) -> KnowledgeReconcileReport:
    """One tick. Never raises: this runs on the worker's housekeeping timer and a
    failure here must not stop a worker.

    Three phases per tenant, each in its own transaction per document: kill what
    two attested syncs agree is gone, reduce what has sat tombstoned past its
    grace window, and -- deliberately -- no purge. A reduced row is an id, three
    uuids, a URI, a reason and three timestamps; dropping it is its own policy
    with its own audit story.
    """
    report = KnowledgeReconcileReport()
    if not get_settings().knowledge_reconcile_enabled:
        return report
    now = now or dt.datetime.now(tz=dt.UTC)

    try:
        tenants = await list_active_tenant_ids()
    except Exception:
        logger.warning("knowledge: could not list tenants; skipping tick", exc_info=True)
        return report

    for tenant_id in tenants:
        try:
            await _sweep_tenant(tenant_id, now=now, report=report)
        except Exception:
            report.failures += 1
            logger.warning("knowledge: reconcile failed for tenant %s", tenant_id, exc_info=True)
        else:
            report.tenants += 1

    if report.documents_tombstoned or report.documents_reduced or report.superseded_reduced:
        logger.info(
            "knowledge: tombstoned %d document(s), reduced %d absent and %d superseded "
            "(%d chunk(s)) across %d tenant(s)",
            report.documents_tombstoned,
            report.documents_reduced,
            report.superseded_reduced,
            report.chunks,
            report.tenants,
        )
    return report
