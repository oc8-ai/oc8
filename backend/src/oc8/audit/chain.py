"""Per-tenant append-only hash chain.

Each event stores ``hash = sha256(prev_hash || canonical(event))`` where
``prev_hash`` is the previous event's hash for the same tenant (genesis = 32
zero bytes). Tampering with any event breaks verification from that link on.

Rows carry a ``mac_version``: 0 is that plain sha256, 1 is HMAC-SHA256 under a
key derived from the root KEK (§12.5), selected by ``audit_mac_enabled``.
Hashing dispatches on each row's OWN version so mixed-version chains verify
across the switch point, and the chain writer refuses to write a version below
the tenant's current head -- an unkeyed row needs no secret to forge.

``append_event`` stages an event on the session; the chain itself is written
when that session's transaction commits (see "Appending" below), so the
per-tenant lock is held only for the end of a transaction, not its whole life.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from oc8.audit.attribution import resolve_responsible
from oc8.auth import Principal
from oc8.config import get_settings
from oc8.models.ops import AuditEvent
from oc8.secrets.keyprovider import audit_mac_key

GENESIS = b"\x00" * 32

logger = logging.getLogger(__name__)


class MacDowngrade(Exception):
    """Refused to append a row whose mac_version is below the tenant's head.

    Raised when audit_mac_enabled has been turned OFF for a tenant whose chain
    is already keyed. Writing the unkeyed row instead would leave the chain
    forgeable from that point on -- the whole point of keying it -- so this
    fails loudly rather than degrading silently. See the design spec's Risks
    section: enabling the flag is a one-way door per deployment.
    """


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()


def _hash(prev: bytes, payload: dict[str, Any], mac_version: int) -> bytes:
    """Hash one chain link at the given mac_version.

    Version 0 is the original unkeyed sha256 and MUST stay byte-identical
    forever -- every row written before §12.5 is stored that way, and any drift
    silently invalidates all of history. Version 1 is HMAC-SHA256 under the
    HKDF-derived audit MAC key, which is what makes a row unforgeable by
    someone holding only DB write access.

    mac_version is required, not defaulted: this is the one module where a
    caller silently getting the legacy unkeyed hash instead of a TypeError
    would corrupt the integrity guarantee rather than just crash. Every call
    site must state its version explicitly.
    """
    data = prev + _canonical(payload)
    if mac_version == 0:
        return hashlib.sha256(data).digest()
    if mac_version == 1:
        return hmac.new(audit_mac_key(), data, hashlib.sha256).digest()
    raise ValueError(f"unknown audit mac_version: {mac_version}")


def _event_payload(
    *,
    tenant_id: uuid.UUID,
    actor_type: str,
    actor_id: uuid.UUID | None,
    category: str,
    action: str,
    resource: dict[str, Any],
    decision: str | None,
    reason: str | None,
    responsible_type: str | None = None,
    responsible_id: str | None = None,
) -> dict[str, Any]:
    """The canonical hashed payload. responsible_* are included ONLY when present,
    so pre-A3 rows (NULL) re-hash exactly as they were originally hashed while new
    rows carry tamper-evident attribution. append_event and verify_chain MUST both
    build the payload through this one function -- any drift silently breaks
    verification of history."""
    payload: dict[str, Any] = {
        "tenant_id": str(tenant_id),
        "actor_type": actor_type,
        "actor_id": str(actor_id) if actor_id else None,
        "category": category,
        "action": action,
        "resource": resource,
        "decision": decision,
        "reason": reason,
    }
    if responsible_type is not None:
        payload["responsible_type"] = responsible_type
        payload["responsible_id"] = responsible_id
    return payload


def recompute_hash(event: AuditEvent, prev_hash: bytes) -> bytes:
    """Recompute an event's hash from its stored columns and a predecessor hash.

    The ONLY supported way to re-derive a hash outside append_event. Verifiers
    must call this rather than rebuilding the payload themselves -- any drift in
    canonicalisation silently invalidates the whole history (see _event_payload).
    """
    payload = _event_payload(
        tenant_id=event.tenant_id,
        actor_type=event.actor_type,
        actor_id=event.actor_id,
        category=event.category,
        action=event.action,
        resource=event.resource,
        decision=event.decision,
        reason=event.reason,
        responsible_type=event.responsible_type,
        responsible_id=event.responsible_id,
    )
    return _hash(prev_hash, payload, event.mac_version)


# --------------------------------------------------------------------------
# Appending: staged on the session, written to the chain at commit.
#
# The chain is serialised per tenant by `pg_advisory_xact_lock(tenant)`, and an
# xact lock is held until the transaction ends. Taking it inside append_event
# (as this module used to) meant an in-process agent run -- one long
# transaction that audits every tool call -- held its tenant's audit lock from
# the first tool call to the end of the run, and every other audited write in
# that tenant (other runs' tool calls, approvals, logins, settings) queued
# behind it; any lock taken after an append was also a deadlock waiting to
# happen.
#
# So append_event only STAGES the event on the session (in call order). A
# `before_commit` listener on the sync `Session` class -- registered once, at
# import of this module, so it covers every session that can ever hold a staged
# event and no commit site can forget it -- writes them inside the committing
# transaction: flush the caller's own rows, lock each tenant (sorted, so two
# sessions staging the same tenants never lock them in opposite orders), read
# the head, hash in order, insert. The lock is therefore held only for the last
# moments of a transaction, and the rows stay atomic with the business writes:
# a rollback discards the staged events, and a rolled-back SAVEPOINT discards
# the events staged inside it (see _drop_rolled_back).
#
# AsyncSession needs nothing special: AsyncSession.commit() runs the sync
# Session.commit() through greenlet_spawn, so the listener's sync
# session.execute()/flush() drive the async driver through await_only exactly
# as every other sync-in-greenlet ORM call does.
# --------------------------------------------------------------------------

_PENDING_KEY = "oc8_pending_audit"

_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended(:tenant, 0))")


@dataclass(eq=False)
class _Staged:
    tenant_id: uuid.UUID
    event: AuditEvent
    payload: dict[str, Any]
    # The innermost SAVEPOINT open when the event was staged (None: the root
    # transaction). Rolling that savepoint, or any savepoint enclosing it, back
    # drops the event.
    scope: SessionTransaction | None


def _pending(session: Session) -> list[_Staged]:
    pending: list[_Staged] = session.info.setdefault(_PENDING_KEY, [])
    return pending


def _write_tenant(session: Session, tenant_id: uuid.UUID, staged: list[_Staged]) -> None:
    """Lock ``tenant_id``'s chain and append ``staged`` to it, in order.

    Advisory lock rather than SELECT ... FOR UPDATE: PostgreSQL requires the
    UPDATE privilege for row locking, which is exactly what migration 0001
    revokes from oc8_app ("permission denied for table audit_event"). Without
    the lock two concurrent writers both read head H and both write
    prev_hash = H -- a fork the verifier reports as a PERMANENT tamper alarm,
    because the append-only table can never be repaired. The lock is
    transaction-scoped: it releases on commit or rollback, no unlock to leak.
    """
    session.execute(_LOCK_SQL, {"tenant": str(tenant_id)})
    target_version = 1 if get_settings().audit_mac_enabled else 0
    head = session.execute(
        select(AuditEvent.hash, AuditEvent.mac_version)
        .where(AuditEvent.tenant_id == tenant_id)
        .order_by(AuditEvent.seq.desc())
        .limit(1)
    ).first()
    prev = head[0] if head is not None else GENESIS
    head_version = head[1] if head is not None else 0
    if head_version > target_version:
        # mac_version is monotonic per tenant. Without this rule an attacker
        # with write access could forge rows marked version 0 -- which need no
        # secret to hash -- and the verifier would accept them.
        raise MacDowngrade(
            f"tenant {tenant_id} chain head is at mac_version {head_version}, "
            f"refusing to append at {target_version} "
            "(audit_mac_enabled was turned off after the chain was keyed)"
        )
    for item in staged:
        ev = item.event
        ev.prev_hash = prev
        ev.hash = _hash(prev, item.payload, target_version)
        ev.mac_version = target_version
        session.add(ev)
        # One flush per row: seq is an identity column, so this is what makes
        # seq order equal chain order within the batch.
        session.flush()
        prev = ev.hash


def _write_staged(session: Session) -> None:
    """Write the session's staged events to their tenants' chains."""
    pending = session.info.pop(_PENDING_KEY, None)
    if not pending:
        return
    by_tenant: dict[uuid.UUID, list[_Staged]] = {}
    for item in pending:
        by_tenant.setdefault(item.tenant_id, []).append(item)
    # Flushed FIRST, and that is a lock-order rule rather than housekeeping:
    # the caller's pending row work must go out BEFORE the tenant lock, never
    # after it. Measured on the old append path: `knowledge/reconcile.py::_hold`
    # dirtied `data_source` and then appended, emitting ['ADVISORY',
    # 'data_source'], while an operator source delete emitted ['data_source',
    # ..., 'ADVISORY'] -- an AB/BA that Postgres aborted ("deadlock detected").
    session.flush()
    # Sorted by the same string the lock hashes, so every session takes a set
    # of tenant locks in one global order.
    for tenant_id in sorted(by_tenant, key=str):
        _write_tenant(session, tenant_id, by_tenant[tenant_id])


@event.listens_for(Session, "before_commit")
def _on_before_commit(session: Session) -> None:
    # before_commit also fires when a SAVEPOINT is released. That is not the
    # commit: the events stay staged (and the lock untaken) until the root
    # transaction commits.
    if session.in_nested_transaction():
        return
    _write_staged(session)


@event.listens_for(Session, "after_soft_rollback")
def _drop_rolled_back(session: Session, previous_transaction: SessionTransaction) -> None:
    pending = session.info.get(_PENDING_KEY)
    if not pending:
        return
    if not previous_transaction.nested:
        pending.clear()
        return

    def _inside(scope: SessionTransaction | None) -> bool:
        while scope is not None:
            if scope is previous_transaction:
                return True
            scope = scope.parent
        return False

    session.info[_PENDING_KEY] = [p for p in pending if not _inside(p.scope)]


@event.listens_for(Session, "after_commit")
def _check_nothing_left(session: Session) -> None:
    # Only reachable if the root transaction was committed directly while a
    # savepoint was still open (before_commit could not tell that commit from a
    # RELEASE). Every savepoint in this code base is context-managed, so this
    # is a tripwire for a future caller, not an expected path.
    if session.in_nested_transaction():
        return
    pending = session.info.pop(_PENDING_KEY, None)
    if pending:
        logger.error(
            "audit: %d staged event(s) were not written: the transaction was "
            "committed while a savepoint was still open",
            len(pending),
        )


@event.listens_for(Session, "after_transaction_end")
def _clear_on_end(session: Session, transaction: SessionTransaction) -> None:
    # The root transaction is over (committed, rolled back, or closed): nothing
    # staged in it can be written any more.
    if transaction.parent is None:
        session.info.pop(_PENDING_KEY, None)


async def append_event(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_type: str,
    actor_id: uuid.UUID | None,
    category: str,
    action: str,
    resource: dict[str, Any] | None = None,
    decision: str | None = None,
    reason: str | None = None,
    principal: Principal | None = None,
    originating_operator: str | None = None,
) -> AuditEvent:
    """Stage an audit event; it is written to the tenant chain when ``session``
    commits, inside the same transaction.

    Takes no lock. It still flushes the caller's pending rows, as it always
    has: the session factories run with autoflush off, and callers written
    against the old append path may rely on that flush. The returned row has no ``seq``, ``hash``,
    ``prev_hash`` or ``mac_version`` until the commit (``expire_on_commit=False``
    sessions can read them off the returned object afterwards). A row staged here is not visible
    to queries in the same transaction before the commit either. A downgrade
    of the chain's mac_version (:class:`MacDowngrade`) surfaces from the commit.
    """
    resource = resource or {}
    r_type, r_id = resolve_responsible(
        tenant_id=tenant_id,
        actor_type=actor_type,
        actor_id=actor_id,
        principal=principal,
        originating_operator=originating_operator,
    )
    payload = _event_payload(
        tenant_id=tenant_id,
        actor_type=actor_type,
        actor_id=actor_id,
        category=category,
        action=action,
        resource=resource,
        decision=decision,
        reason=reason,
        responsible_type=r_type,
        responsible_id=r_id,
    )
    ev = AuditEvent(
        tenant_id=tenant_id,
        actor_type=actor_type,
        actor_id=actor_id,
        category=category,
        action=action,
        resource=resource,
        decision=decision,
        reason=reason,
        responsible_type=r_type,
        responsible_id=r_id,
    )
    await session.flush()
    sync = session.sync_session
    _pending(sync).append(
        _Staged(tenant_id=tenant_id, event=ev, payload=payload, scope=sync.get_nested_transaction())
    )
    return ev


async def flush_audit(session: AsyncSession) -> None:
    """Write the session's staged audit events to the chain NOW, instead of at
    commit.

    The exception path: it takes the tenant's advisory lock immediately, and
    that lock is then held until the transaction ends -- exactly what staging
    avoids -- so never call it from a long transaction (an agent run). It exists
    for code that must read its own events back, or their ``seq``/``hash``,
    inside the transaction that appended them (tests, chain maintenance).

    Refused inside a SAVEPOINT: events staged outside it would be written into
    it, and a rollback of the savepoint would then lose them although the
    transaction that staged them commits.
    """
    if session.in_nested_transaction():
        raise RuntimeError("flush_audit() inside a savepoint would tie its events to it")
    await session.run_sync(_write_staged)


async def verify_chain(session: AsyncSession, tenant_id: uuid.UUID) -> bool:
    events = (
        (
            await session.execute(
                select(AuditEvent)
                .where(AuditEvent.tenant_id == tenant_id)
                .order_by(AuditEvent.seq.asc())
            )
        )
        .scalars()
        .all()
    )
    prev = GENESIS
    for ev in events:
        if ev.prev_hash != prev:
            return False
        if not hmac.compare_digest(recompute_hash(ev, prev), ev.hash):
            return False
        prev = ev.hash
    return True
