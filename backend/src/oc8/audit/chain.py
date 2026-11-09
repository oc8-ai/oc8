"""Per-tenant append-only hash chain.

Each event stores ``hash = sha256(prev_hash || canonical(event))`` where
``prev_hash`` is the previous event's hash for the same tenant (genesis = 32
zero bytes). Tampering with any event breaks verification from that link on.

Rows carry a ``mac_version``: 0 is that plain sha256, 1 is HMAC-SHA256 under a
key derived from the root KEK (§12.5), selected by ``audit_mac_enabled``.
Hashing dispatches on each row's OWN version so mixed-version chains verify
across the switch point, and ``append_event`` refuses to write a version below
the tenant's current head -- an unkeyed row needs no secret to forge.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from oc8.audit.attribution import resolve_responsible
from oc8.auth import Principal
from oc8.config import get_settings
from oc8.models.ops import AuditEvent
from oc8.secrets.keyprovider import audit_mac_key

GENESIS = b"\x00" * 32


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


async def lock_tenant_audit(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """The tenant's audit-chain lock, until commit; reentrant within one
    transaction. Flushes first (see `append_event`).

    Lock order (Ruling 60): a transaction that also takes a narrower advisory
    lock (`oc8.runtime.run_inputs.lock_run_inputs`) takes THIS one first, even
    if it appends only later -- otherwise one transaction holds this and waits
    for the narrower lock while another holds the narrower one and waits here
    in `append_event`."""
    await session.flush()
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:tenant, 0))"),
        {"tenant": str(tenant_id)},
    )


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
    resource = resource or {}
    r_type, r_id = resolve_responsible(
        tenant_id=tenant_id,
        actor_type=actor_type,
        actor_id=actor_id,
        principal=principal,
        originating_operator=originating_operator,
    )
    # Serialise appends per tenant. The head read below is followed by an await
    # before flush(), so two concurrent appends for the same tenant would both
    # read head H and both write prev_hash = H -- forking the chain and making
    # the verifier report a false, PERMANENT tamper alarm (audit_event is
    # append-only: migration 0001 REVOKEs UPDATE/DELETE from oc8_app, so the
    # forked rows can never be repaired and verify_full re-detects them
    # forever).
    #
    # Advisory lock rather than SELECT ... FOR UPDATE: PostgreSQL requires the
    # UPDATE privilege for row locking, which is exactly what migration 0001
    # revokes, so FOR UPDATE fails at runtime with "permission denied for
    # table audit_event" (verified empirically against oc8_app). The lock is
    # transaction-scoped, so it releases on commit or rollback with no
    # unlock path to leak.
    #
    # Flushed FIRST, and that is a lock-order rule rather than housekeeping. The
    # statement below is a bare TextClause, which SQLAlchemy does not autoflush,
    # so a caller's pending row UPDATE went out on the ORM SELECT further down --
    # i.e. AFTER this lock. Measured: `knowledge/reconcile.py::_hold` dirtied
    # `data_source` and then appended, emitting ['ADVISORY', 'data_source'],
    # while an operator source delete emits ['data_source', ..., 'ADVISORY'] --
    # a genuine AB/BA that Postgres aborted ("deadlock detected"). Flushing here
    # costs nothing (the SELECT below would have flushed anyway) and makes "the
    # append takes the tenant lock after all of the caller's row work is already
    # emitted" true by construction for every caller of this function.
    await lock_tenant_audit(session, tenant_id)
    target_version = 1 if get_settings().audit_mac_enabled else 0
    head = (
        await session.execute(
            select(AuditEvent.hash, AuditEvent.mac_version)
            .where(AuditEvent.tenant_id == tenant_id)
            .order_by(AuditEvent.seq.desc())
            .limit(1)
        )
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
    event = AuditEvent(
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
        prev_hash=prev,
        hash=_hash(prev, payload, target_version),
        mac_version=target_version,
    )
    session.add(event)
    await session.flush()
    return event


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
