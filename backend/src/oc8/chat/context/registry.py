"""One resolver per kind, and the one door every pinned ref goes through.

Unknown kinds, malformed ids, other tenants', deleted and unauthorised objects
are all DROPPED, silently and identically, so a ref never confirms to a member
that something exists which they may not see. The order of the surviving refs
is kept; a repeated ref is kept once."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oc8.chat.context.base import ContextResolver, Viewer

RESOLVERS: dict[str, ContextResolver] = {}


def register(resolver: ContextResolver) -> ContextResolver:
    RESOLVERS[resolver.kind] = resolver
    return resolver


@dataclass(frozen=True)
class ResolvedRef:
    kind: str
    id: str
    label: str
    #: The object's classification (`public` .. `restricted`), for a later
    #: check against the model or the PDP. Nothing here enforces it.
    classification: str
    readable: bool
    size: int | None
    deep_link: str | None


async def resolve_ref(
    db: AsyncSession, *, viewer: Viewer, kind: str, ref_id: str
) -> tuple[ResolvedRef, Any] | None:
    """The ref and its loaded object, or None for every kind of refusal."""
    if viewer.member.deleted_at is not None:
        # An offboarded member's stored rows authorise nothing (the same
        # predicate `scope_for_member` applies).
        return None
    resolver = RESOLVERS.get(kind)
    if resolver is None:
        return None
    try:
        obj = await resolver.load(db, viewer.member.tenant_id, ref_id)
    except (ValueError, TypeError):
        return None
    if obj is None or not await resolver.authorize(db, viewer, obj):
        return None
    size = getattr(obj, "size_bytes", None)
    return (
        ResolvedRef(
            kind=kind,
            id=ref_id,
            label=resolver.label(obj),
            classification=resolver.classification(obj),
            readable=resolver.readable,
            size=size if isinstance(size, int) else None,
            deep_link=resolver.deep_link(obj),
        ),
        obj,
    )


async def resolve_refs(
    db: AsyncSession, *, viewer: Viewer, refs: Sequence[Mapping[str, Any]]
) -> list[ResolvedRef]:
    out: list[ResolvedRef] = []
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        kind, ref_id = str(ref.get("kind") or ""), str(ref.get("id") or "")
        if (kind, ref_id) in seen:
            continue
        seen.add((kind, ref_id))
        resolved = await resolve_ref(db, viewer=viewer, kind=kind, ref_id=ref_id)
        if resolved is not None:
            out.append(resolved[0])
    return out
