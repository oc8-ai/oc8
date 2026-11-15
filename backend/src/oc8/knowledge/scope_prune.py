"""What an admin takes out of a source's scope leaves on the next sync.

Reconciliation (`oc8.knowledge.reconcile`) exists because a listing that no
longer contains a document is usually NOT the customer deleting it: an expired
token, a revoked policy and a folder id that stopped resolving all look exactly
like "everything is gone". So it waits for two attested syncs a window apart and
refuses a listing that loses too much at once.

None of that doubt applies here. Narrowing the picker in the edit drawer is the
customer saying, in so many words, "not these any more". The PATCH that saves it
records a `knowledge.source_scope_changed` audit event and sets
`cursor["scope_changed"] = {"by": <the admin's subject>}`, and the next sync
prunes at once -- no confirm window, no vanish-ratio hold, and whether or not
reconciliation is enabled. (A bare `True` flag, written before the flag carried
its author, is still honoured; its prune is then attributed to the system.)

What it still needs is the one fact reconcile needs too: an AUTHORITATIVE
listing of what the narrowed source now holds. Without it "absent from this
listing" means nothing, so every other outcome (`NONE`, `REMOVALS`, an exception,
a timeout) prunes nothing and keeps the flag for the next sync. The flag is
cleared only in the savepoint that pruned.

A NON-EMPTY listing prunes and REDUCES at once (`reduce_now=True`): the
de-selected text leaves oc8 in the same transaction, with one
`knowledge.document_deleted` entry per document naming the admin whose edit
licensed it and `deleted_reason="scope_changed"`. Waiting for a grace window
would leave it to the reconcile sweep, which may be switched off, and the edit
was deliberate.

An EMPTY listing prunes everything, but recoverably (`reduce_now=False`). Every
create and edit refuses a picker that selects nothing, and the attestation
contract makes a connector raise rather than attest an empty set on an error --
yet "everything is gone" is still the one answer a broken credential could fake.
So the documents leave retrieval at once and keep their text: `restore_document`
can bring them back, the reconcile sweep reduces them after its grace window,
and the sync records the `scope_emptied` note so the source shows what happened.

Neither is suppressed: re-selecting a document ingests it again, and the cursor
eviction below makes sure the hash cursor does not skip it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from collections.abc import Collection, Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.audit.chain import append_event
from oc8.auth.principal import Principal
from oc8.knowledge.chunks import live_chunks
from oc8.knowledge.connectors.base import Attestation, AuthContext, SourceListing
from oc8.knowledge.connectors.report import report_note
from oc8.knowledge.tombstone import CATEGORY, SCOPE_CHANGED, tombstone_document

logger = logging.getLogger(__name__)

#: The cursor key a scope-narrowing edit sets and a successful prune clears.
FLAG = "scope_changed"
#: Audit action an admin's scope-changing edit records.
SCOPE_CHANGED_EVENT = "knowledge.source_scope_changed"
#: The sync note recorded when a scope change emptied the source.
SCOPE_EMPTIED_NOTE = "scope_emptied"
#: The schema marker that makes a config property part of a source's scope.
PICKER = "x-oc8-picker"

# The same bound reconcile puts on the same call: a hung remote must not hold
# the sync's transaction open.
_ATTEST_TIMEOUT = 30.0


def _pickers(schema: Mapping[str, Any] | None) -> list[str]:
    properties = (schema or {}).get("properties") or {}
    return [
        name
        for name, spec in properties.items()
        if isinstance(spec, Mapping) and spec.get(PICKER) is True
    ]


def picker_changes(
    schema: Mapping[str, Any] | None, before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """The picker properties a config edit changed, as {name: {before, after}}.

    Picker values are selections -- ids and labels -- which is what makes them
    safe to put in an audit entry; nothing else of the config goes there.
    """
    return {
        name: {"before": before.get(name), "after": after.get(name)}
        for name in _pickers(schema)
        if before.get(name) != after.get(name)
    }


def picker_changed(
    schema: Mapping[str, Any] | None, before: Mapping[str, Any], after: Mapping[str, Any]
) -> bool:
    """Whether a config edit changed any property the schema marks as a picker."""
    return bool(picker_changes(schema, before, after))


def empty_pickers(schema: Mapping[str, Any] | None, config: Mapping[str, Any]) -> list[str]:
    """Picker properties a config leaves missing or empty (any falsy value).

    A source that selects nothing is a source to delete, not to narrow.
    """
    return [name for name in _pickers(schema) if not config.get(name)]


def scope_flag(principal: Principal) -> dict[str, str]:
    """The cursor flag value: who narrowed the scope."""
    return {"by": principal.subject}


def _flag_author(flag: Any) -> str | None:
    """The admin a flag names, or None for a bare `True` from before it did."""
    if isinstance(flag, Mapping):
        by = flag.get("by")
        return str(by) if by else None
    return None


async def record_scope_change(
    db: AsyncSession,
    *,
    principal: Principal,
    data_source: m.DataSource,
    changes: dict[str, dict[str, Any]],
) -> None:
    """Stage the audit event for an admin's scope-changing edit. flush-only."""
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category=CATEGORY,
        action=SCOPE_CHANGED_EVENT,
        resource={
            "data_source_id": str(data_source.id),
            "connector_type": data_source.connector_type,
            "scope": changes,
        },
        decision="allow",
        reason="an operator changed the source's selected scope",
        principal=principal,
    )


async def _live_documents(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    data_source: m.DataSource,
    kb_ids: Collection[uuid.UUID] | None = None,
) -> list[tuple[uuid.UUID, str]]:
    """(kb_id, source_uri) of every live document this source holds."""
    query = (
        live_chunks()
        .with_only_columns(m.KbChunk.kb_id, m.KbChunk.source_uri)
        .where(
            m.KbChunk.tenant_id == tenant_id,
            m.KbChunk.data_source_id == data_source.id,
        )
    )
    if kb_ids is not None:
        query = query.where(m.KbChunk.kb_id.in_(list(kb_ids)))
    rows = (await db.execute(query.distinct())).all()
    return sorted(((kb, uri) for kb, uri in rows), key=lambda r: (str(r[0]), r[1] or ""))


async def prune_out_of_scope(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    data_source: m.DataSource,
    kb_ids: Collection[uuid.UUID],
    present_uris: frozenset[str],
    now: dt.datetime,
    reduce_now: bool = True,
    actor: str | None = None,
) -> int:
    """Tombstone every live document of this source in these bases whose URI the
    listing does not contain. Returns how many (base, document) pairs it removed.

    `actor` is the subject of the admin whose edit licensed the prune; a reducing
    prune's ledger entries name them. flush-only; the caller owns the commit.
    """
    if not kb_ids:
        return 0
    pruned = 0
    for kb_id, uri in await _live_documents(
        db, tenant_id=tenant_id, data_source=data_source, kb_ids=kb_ids
    ):
        if uri in present_uris:
            continue
        removal = await tombstone_document(
            db,
            tenant_id=tenant_id,
            kb_id=kb_id,
            data_source_id=data_source.id,
            source_uri=uri,
            reason=SCOPE_CHANGED,
            reduce_now=reduce_now,
            now=now,
            originating_operator=actor,
        )
        if removal is not None:
            pruned += removal.documents
    return pruned


async def apply_scope_change(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    data_source: m.DataSource,
    connector: Any,
    auth: AuthContext | None,
    now: dt.datetime,
) -> int | None:
    """Prune what a narrowed selection left out, if this sync can prove it.

    Returns the number pruned, or None when the flag is not set or the listing
    cannot license a prune (the flag then stays set). `run_source_sync` calls
    this only after a sync with no connector-level failure, with its sync-report
    collector installed so the `scope_emptied` note lands on the job. flush-only.
    """
    # Re-read under the row lock, and the CONFIG with it: an edit committed
    # while this sync was fetching set the flag for a selection the in-memory
    # config predates, and attesting the old selection would clear the flag
    # without pruning what the edit took out.
    await db.refresh(data_source, ["cursor", "config"], with_for_update=True)
    flag = (data_source.cursor or {}).get(FLAG)
    if not flag:
        return None

    listing = await _attest(connector, data_source, auth)
    if listing is None or listing.attestation is not Attestation.AUTHORITATIVE:
        return None
    present = listing.present_uris
    emptied = not present

    # Every base this source feeds, not only the one this sync was queued for:
    # the listing is a claim about the source.
    live = await _live_documents(db, tenant_id=tenant_id, data_source=data_source)
    pruned_uris = {uri for _, uri in live} - present
    pruned = await prune_out_of_scope(
        db,
        tenant_id=tenant_id,
        data_source=data_source,
        kb_ids={kb for kb, _ in live},
        present_uris=present,
        now=now,
        reduce_now=not emptied,
        actor=_flag_author(flag),
    )

    cursor = dict(data_source.cursor or {})
    cursor.pop(FLAG, None)
    # Forget the pruned documents' digests, so re-selecting one later brings it
    # back instead of the hash cursor skipping it for ever. A digest stays while
    # any other URI still maps to it (identical bytes share one digest).
    uri_hashes = dict(cursor.get("uri_hashes") or {})
    dropped = {uri_hashes.pop(uri) for uri in pruned_uris if uri in uri_hashes}
    if dropped:
        gone = dropped - set(uri_hashes.values())
        cursor["uri_hashes"] = uri_hashes
        cursor["hashes"] = [h for h in cursor.get("hashes") or [] if h not in gone]
    # Bare JSONB with no MutableDict: reassign the whole dict.
    data_source.cursor = cursor
    await db.flush()
    # `tombstone_document` recounts with a set-based UPDATE.
    await db.refresh(data_source, ["doc_count"])
    if emptied:
        logger.warning(
            "knowledge: source %s attested an empty listing after a scope change; "
            "%d document(s) tombstoned, restorable until the grace window ends",
            data_source.id,
            pruned,
        )
        report_note(SCOPE_EMPTIED_NOTE)
    return pruned


async def _attest(
    connector: Any, data_source: m.DataSource, auth: AuthContext | None
) -> SourceListing | None:
    """The connector's listing, or None for every way of not having one."""
    attest = getattr(connector, "attest_listing", None)
    if not callable(attest):
        return None
    try:
        async with asyncio.timeout(_ATTEST_TIMEOUT):
            listing = await attest(data_source.config, auth)
    except Exception:
        logger.warning(
            "knowledge: source %s could not attest its listing after a scope change",
            data_source.id,
            exc_info=True,
        )
        return None
    return listing if isinstance(listing, SourceListing) else None
