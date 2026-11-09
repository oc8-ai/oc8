"""Immutable audit trail with a per-tenant hash chain (tech-spec §12.5)."""

from oc8.audit.attribution import resolve_responsible
from oc8.audit.chain import append_event, verify_chain

__all__ = ["append_event", "resolve_responsible", "verify_chain"]
