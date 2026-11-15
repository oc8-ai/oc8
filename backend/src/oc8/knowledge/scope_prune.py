"""What an admin takes out of a source's scope leaves on the next sync.

Reconciliation (`oc8.knowledge.reconcile`) exists because a listing that no
longer contains a document is usually NOT the customer deleting it: an expired
token, a revoked policy and a folder id that stopped resolving all look exactly
like "everything is gone". So it waits for two attested syncs a window apart and
refuses a listing that loses too much at once.

None of that doubt applies here. Narrowing the picker in the edit drawer is the
customer saying, in so many words, "not these any more". The PATCH that saves it
sets `cursor["scope_changed"]`, and the next sync prunes at once -- no confirm
window, no vanish-ratio hold, and whether or not reconciliation is enabled.

What it still needs is the one fact reconcile needs too: an AUTHORITATIVE
listing of what the narrowed source now holds. Without it "absent from this
listing" means nothing, so every other outcome (`NONE`, `REMOVALS`, an exception,
a timeout) prunes nothing and keeps the flag for the next sync. The flag is
cleared only in the savepoint that pruned.

An EMPTY authoritative listing prunes everything. The PATCH refuses a picker
that would select nothing, and the attestation contract requires a connector to
raise rather than attest an empty set on an error, so empty here means empty.

The prune REDUCES at once (`reduce_now=True`): the de-selected text leaves oc8 in
the same transaction, with a `knowledge.document_deleted` entry per document
naming the system and the reason. Waiting for a grace window would leave it to
the reconcile sweep, which may be switched off, and the edit was deliberate. It
is not suppressed: re-selecting a document ingests it again, and the cursor
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
from oc8.knowledge.chunks import live_chunks
from oc8.knowledge.connectors.base import Attestation, AuthContext, SourceListing
from oc8.knowledge.tombstone import SCOPE_CHANGED, tombstone_document

logger = logging.getLogger(__name__)

#: The cursor key a scope-narrowing edit sets and a successful prune clears.
FLAG = "scope_changed"
#: The schema marker that makes a config property part of a source's scope.
PICKER = "x-oc8-picker"

# The same bound reconcile puts on the same call: a hung remote must not hold
# the sync's transaction open.
_ATTEST_TIMEOUT = 30.0


def picker_changed(
    schema: Mapping[str, Any] | None, before: Mapping[str, Any], after: Mapping[str, Any]
) -> bool:
    """Whether a config edit changed any property the schema marks as a picker."""
    properties = (schema or {}).get("properties") or {}
    return any(
        isinstance(spec, Mapping)
        and spec.get(PICKER) is True
        and before.get(name) != after.get(name)
        for name, spec in properties.items()
    )


def empty_pickers(schema: Mapping[str, Any] | None, config: Mapping[str, Any]) -> list[str]:
    """Picker properties a config leaves missing or empty.

    A source that selects nothing is a source to delete, not to narrow, and it
    is what lets the sync take an empty AUTHORITATIVE listing at its word.
    """
    properties = (schema or {}).get("properties") or {}
    return [
        name
        for name, spec in properties.items()
        if isinstance(spec, Mapping)
        and spec.get(PICKER) is True
        and (config.get(name) is None or config.get(name) == [])
    ]


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
) -> int:
    """Tombstone every live document of this source in these bases whose URI the
    listing does not contain. Returns how many (base, document) pairs it removed.

    flush-only; the caller owns the commit.
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
            reduce_now=True,
            now=now,
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
    this only after a sync with no connector-level failure. flush-only.
    """
    # Re-read under the row lock, and the CONFIG with it: an edit committed
    # while this sync was fetching set the flag for a selection the in-memory
    # config predates, and attesting the old selection would clear the flag
    # without pruning what the edit took out.
    await db.refresh(data_source, ["cursor", "config"], with_for_update=True)
    if not (data_source.cursor or {}).get(FLAG):
        return None

    listing = await _attest(connector, data_source, auth)
    if listing is None or listing.attestation is not Attestation.AUTHORITATIVE:
        return None
    present = listing.present_uris

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
