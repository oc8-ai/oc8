"""Pinned context on a chat or a project, and the member's chat projects
(oc8 Chat spec §5.2, §5.5).

A pin is the member's explicit release, so only what the member can see may be
pinned, and every pin is re-checked for whoever looks at it later. Both go
through the context registry's one door (`resolve_ref`).

`ChatProject.owner_member_id` and `ChatSession.project_id` are soft references
(no foreign keys): ownership is checked here on every read, deleting a project
detaches its chats in the same transaction, and an offboarded member's projects
are removed with the rest of what they owned (`delete_member_projects`)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.chat.context.base import Viewer
from oc8.chat.context.registry import resolve_ref


@dataclass(frozen=True)
class PinView:
    """One stored pin as one viewer sees it. An unavailable pin keeps the label
    stored when it was pinned (the UI shows "Source not available")."""

    kind: str
    id: str
    label: str
    available: bool


class PinRefused(Exception):
    """Some refs are not the member's to pin. `refused` names each by the kind
    and id the client sent -- never a label -- and a ref that does not exist
    reads exactly like one the member may not see."""

    def __init__(self, refused: list[dict[str, str]]) -> None:
        super().__init__("some references are not available to you")
        self.refused = refused


def _kind_and_id(ref: Mapping[str, Any]) -> tuple[str, str]:
    return str(ref.get("kind") or ""), str(ref.get("id") or "")


async def pinned_refs_for_viewer(
    db: AsyncSession, *, viewer: Viewer, refs: Sequence[Mapping[str, Any]]
) -> list[PinView]:
    out: list[PinView] = []
    for ref in refs:
        kind, ref_id = _kind_and_id(ref)
        resolved = await resolve_ref(db, viewer=viewer, kind=kind, ref_id=ref_id)
        out.append(
            PinView(
                kind=kind,
                id=resolved[0].id if resolved else ref_id,
                label=resolved[0].label if resolved else str(ref.get("label") or ""),
                available=resolved is not None,
            )
        )
    return out


async def authorise_pins(
    db: AsyncSession,
    *,
    viewer: Viewer,
    refs: Sequence[Mapping[str, Any]],
    already: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, str]]:
    """The refs to store: canonical id and the row's own label, each kept once.
    A ref already in `already` (the chat's or project's stored pins, matched by
    kind and canonical id) is kept as stored without a fresh check: removing
    one pin must never fail because another has since become unavailable, and
    every stored pin is re-checked whenever it is read. Raises `PinRefused`
    when any NEW ref is not the viewer's to see."""
    previous: dict[tuple[str, str], dict[str, str]] = {}
    for ref in already:
        if isinstance(ref, Mapping):
            kind, ref_id = _kind_and_id(ref)
            previous.setdefault(
                (kind, ref_id), {"kind": kind, "id": ref_id, "label": str(ref.get("label") or "")}
            )
    stored: list[dict[str, str]] = []
    kept: set[tuple[str, str]] = set()
    refused: list[dict[str, str]] = []
    for ref in refs:
        kind, ref_id = _kind_and_id(ref)
        known = previous.get((kind, ref_id))
        if known is not None:
            if (kind, ref_id) not in kept:
                kept.add((kind, ref_id))
                stored.append(dict(known))
            continue
        resolved = await resolve_ref(db, viewer=viewer, kind=kind, ref_id=ref_id)
        if resolved is None:
            refused.append({"kind": kind, "id": ref_id})
            continue
        canonical = resolved[0]
        if (kind, canonical.id) in kept:
            continue
        kept.add((kind, canonical.id))
        stored.append({"kind": kind, "id": canonical.id, "label": canonical.label})
    if refused:
        raise PinRefused(refused)
    return stored


async def owned_project(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID, project_id: uuid.UUID
) -> m.ChatProject | None:
    """The project, when it is this member's in this tenant; None otherwise --
    missing and someone else's read the same."""
    return (
        await db.execute(
            select(m.ChatProject).where(
                m.ChatProject.id == project_id,
                m.ChatProject.tenant_id == tenant_id,
                m.ChatProject.owner_member_id == member_id,
            )
        )
    ).scalar_one_or_none()


async def list_projects(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID
) -> list[m.ChatProject]:
    rows = await db.execute(
        select(m.ChatProject)
        .where(m.ChatProject.tenant_id == tenant_id, m.ChatProject.owner_member_id == member_id)
        .order_by(m.ChatProject.name, m.ChatProject.id)
    )
    return list(rows.scalars())


async def delete_project(db: AsyncSession, *, project: m.ChatProject) -> None:
    """Delete `project` and detach its chats, in the caller's transaction."""
    await db.execute(
        update(m.ChatSession)
        .where(
            m.ChatSession.tenant_id == project.tenant_id,
            m.ChatSession.project_id == project.id,
        )
        .values(project_id=None)
    )
    await db.delete(project)
    await db.flush()


async def delete_member_projects(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID
) -> None:
    """Remove every project `member_id` owns and detach the chats filed in
    them. Part of offboarding (`copilot.profile.offboard_member`)."""
    owned = select(m.ChatProject.id).where(
        m.ChatProject.tenant_id == tenant_id, m.ChatProject.owner_member_id == member_id
    )
    await db.execute(
        update(m.ChatSession)
        .where(m.ChatSession.tenant_id == tenant_id, m.ChatSession.project_id.in_(owned))
        .values(project_id=None)
    )
    await db.execute(
        delete(m.ChatProject).where(
            m.ChatProject.tenant_id == tenant_id, m.ChatProject.owner_member_id == member_id
        )
    )
    await db.flush()
