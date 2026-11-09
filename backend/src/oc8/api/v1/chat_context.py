"""Pinned context, chat projects, `#` search and source browsing (oc8 Chat spec §5).

Every route admits through the chat door. What the caller may then see is the
context registry's answer for THIS member (`chat.context`), never a second rule
written here. Pins, a chat's project and its pinned state are written only by
the chat's owner (`session_owned_by_caller`, no oversight carve-out), and a
project is visible to its owner alone."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Query, status

from oc8 import models as m
from oc8.api.deps import DbSession, require_departmental, require_permission
from oc8.api.v1._chat_common import (
    actor_viewer,
    ref_views,
    session_detail,
    session_owned_by_caller,
)
from oc8.audit import append_event
from oc8.authz.permissions import AGENT, COPILOT_USE, KNOWLEDGE, VIEW, perm
from oc8.authz.scope import HumanActor
from oc8.chat.context.base import Viewer
from oc8.chat.context.registry import RESOLVERS, resolve_ref
from oc8.chat.projects import (
    PinRefused,
    authorise_pins,
    delete_project,
    list_projects,
    owned_project,
)
from oc8.knowledge.browse import BrowseUnsupported, InvalidReference, browse_source
from oc8.knowledge.connectors.base import ConnectorError
from oc8.schemas.dto import (
    BrowseEntryDTO,
    BrowseListingDTO,
    ChatProjectDTO,
    ChatSessionDTO,
    ContextSearchItemDTO,
)
from oc8.schemas.requests import (
    ChatContextRefRequest,
    ChatProjectRequest,
    PinRefsRequest,
    SetPinnedRequest,
    SetProjectRequest,
)

router = APIRouter()

_Actor = Annotated[
    HumanActor, Depends(require_departmental(perm(AGENT, VIEW), or_tenant_wide=COPILOT_USE))
]

#: `#` search bounds: the query's length, how many kinds one call may name,
#: hits per kind, and hits in all.
MAX_SEARCH_QUERY: Final = 100
MAX_SEARCH_KINDS: Final = 16
SEARCH_PER_KIND: Final = 8
MAX_SEARCH_RESULTS: Final = 30

_NOT_BROWSABLE = "This source can't be browsed — pin it as a whole."


async def _authorised(
    db: DbSession,
    viewer: Viewer,
    refs: list[ChatContextRefRequest],
    already: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """The refs to store, or a 422 naming each refused one by the id the
    client sent. A ref that does not exist is refused exactly like one the
    caller may not see, so the answer confirms nothing across tenants. Refs
    in `already` (what is stored now) are kept without a fresh check."""
    try:
        return await authorise_pins(
            db,
            viewer=viewer,
            refs=[{"kind": r.kind, "id": r.id} for r in refs],
            already=list(already or []),
        )
    except PinRefused as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"message": "not available to you", "refused": exc.refused},
        ) from exc


async def _session_reply(db: DbSession, viewer: Viewer, session: m.ChatSession) -> ChatSessionDTO:
    """The chat as `GET /chat/sessions/{id}` returns it, so a client writing
    this reply into its cache keeps the active runs and parked question."""
    await db.flush()
    dto = await session_detail(db, viewer=viewer, session=session)
    await db.commit()
    return dto


@router.put("/chat/sessions/{session_id}/pins", response_model=ChatSessionDTO)
async def put_pins(
    session_id: uuid.UUID, body: PinRefsRequest, db: DbSession, actor: _Actor
) -> ChatSessionDTO:
    session = await session_owned_by_caller(session_id, db, actor)
    viewer = await actor_viewer(db, actor)
    session.pinned_refs = await _authorised(
        db, viewer, body.refs, already=list(session.pinned_refs or [])
    )
    return await _session_reply(db, viewer, session)


@router.put("/chat/sessions/{session_id}/project", response_model=ChatSessionDTO)
async def put_project(
    session_id: uuid.UUID, body: SetProjectRequest, db: DbSession, actor: _Actor
) -> ChatSessionDTO:
    session = await session_owned_by_caller(session_id, db, actor)
    if body.project_id is not None:
        await _own_project_or_404(db, actor, body.project_id)
    session.project_id = body.project_id
    return await _session_reply(db, await actor_viewer(db, actor), session)


@router.put("/chat/sessions/{session_id}/pinned", response_model=ChatSessionDTO)
async def put_pinned(
    session_id: uuid.UUID, body: SetPinnedRequest, db: DbSession, actor: _Actor
) -> ChatSessionDTO:
    session = await session_owned_by_caller(session_id, db, actor)
    session.pinned = body.pinned
    return await _session_reply(db, await actor_viewer(db, actor), session)


async def _project_dto(db: DbSession, viewer: Viewer, project: m.ChatProject) -> ChatProjectDTO:
    return ChatProjectDTO(
        id=str(project.id),
        name=project.name,
        instructions=project.instructions,
        pinned_refs=await ref_views(db, viewer, list(project.pinned_refs or [])),
        created_at=project.created_at.isoformat(),
    )


async def _own_project_or_404(
    db: DbSession, actor: HumanActor, project_id: uuid.UUID
) -> m.ChatProject:
    project = await owned_project(
        db, tenant_id=actor.principal.tenant_id, member_id=actor.member.id, project_id=project_id
    )
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    return project


async def _audit_project(
    db: DbSession, actor: HumanActor, project: m.ChatProject, action: str
) -> None:
    """`chat.project.<action>`, naming the project by id and name. Never its
    instructions, which are the member's own words."""
    await append_event(
        db,
        tenant_id=actor.principal.tenant_id,
        actor_type="operator",
        actor_id=actor.member.id,
        category="chat",
        action=f"chat.project.{action}",
        resource={"project_id": str(project.id), "name": project.name},
        principal=actor.principal,
    )


@router.get("/chat/projects", response_model=list[ChatProjectDTO])
async def get_projects(db: DbSession, actor: _Actor) -> list[ChatProjectDTO]:
    rows = await list_projects(db, tenant_id=actor.principal.tenant_id, member_id=actor.member.id)
    viewer = await actor_viewer(db, actor)
    return [await _project_dto(db, viewer, p) for p in rows]


@router.post("/chat/projects", response_model=ChatProjectDTO, status_code=status.HTTP_201_CREATED)
async def create_project(body: ChatProjectRequest, db: DbSession, actor: _Actor) -> ChatProjectDTO:
    viewer = await actor_viewer(db, actor)
    project = m.ChatProject(
        tenant_id=actor.principal.tenant_id,
        owner_member_id=actor.member.id,
        name=body.name,
        instructions=body.instructions,
        pinned_refs=await _authorised(db, viewer, body.pinned_refs),
    )
    db.add(project)
    await db.flush()
    await _audit_project(db, actor, project, "created")
    dto = await _project_dto(db, viewer, project)
    await db.commit()
    return dto


@router.patch("/chat/projects/{project_id}", response_model=ChatProjectDTO)
async def update_project(
    project_id: uuid.UUID, body: ChatProjectRequest, db: DbSession, actor: _Actor
) -> ChatProjectDTO:
    """`name` is required; `instructions` and `pinnedRefs` change only when sent."""
    project = await _own_project_or_404(db, actor, project_id)
    viewer = await actor_viewer(db, actor)
    sent = body.model_fields_set
    if "pinned_refs" in sent:
        project.pinned_refs = await _authorised(
            db, viewer, body.pinned_refs, already=list(project.pinned_refs or [])
        )
    if "instructions" in sent:
        project.instructions = body.instructions
    project.name = body.name
    await db.flush()
    await _audit_project(db, actor, project, "updated")
    dto = await _project_dto(db, viewer, project)
    await db.commit()
    return dto


@router.delete("/chat/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_project(project_id: uuid.UUID, db: DbSession, actor: _Actor) -> None:
    """Delete the project; its chats stay, detached, in the same transaction."""
    project = await _own_project_or_404(db, actor, project_id)
    await _audit_project(db, actor, project, "deleted")
    await delete_project(db, project=project)
    await db.commit()


@router.get("/chat/context/search", response_model=list[ContextSearchItemDTO])
async def search_context(
    db: DbSession,
    actor: _Actor,
    q: Annotated[str, Query(max_length=MAX_SEARCH_QUERY)] = "",
    kinds: Annotated[str | None, Query(max_length=1000)] = None,
) -> list[ContextSearchItemDTO]:
    """What the caller could pin, by name. Each kind's own `search` decides
    what this member sees. This feeds the member's own picker, not the model,
    so restricted items may appear here."""
    asked = [k.strip() for k in kinds.split(",") if k.strip()] if kinds else list(RESOLVERS)
    if len(asked) > MAX_SEARCH_KINDS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "too many kinds")
    unknown = sorted({k for k in asked if k not in RESOLVERS})
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown kinds: {', '.join(unknown)}"
        )
    viewer = await actor_viewer(db, actor)
    query = q.strip()
    items: list[ContextSearchItemDTO] = []
    for kind in dict.fromkeys(asked):
        resolver = RESOLVERS[kind]
        for obj in await resolver.search(db, viewer, query, SEARCH_PER_KIND):
            items.append(
                ContextSearchItemDTO(
                    kind=kind,
                    id=resolver.ref_id(obj),
                    label=resolver.label(obj),
                    connected=obj.connected if isinstance(obj, m.DataSource) else True,
                )
            )
            if len(items) >= MAX_SEARCH_RESULTS:
                return items
    return items


@router.get(
    "/chat/data-sources/{source_id}/browse",
    response_model=BrowseListingDTO,
    dependencies=[Depends(require_permission(perm(KNOWLEDGE, VIEW)))],
)
async def browse(
    source_id: uuid.UUID,
    db: DbSession,
    actor: _Actor,
    path: Annotated[str, Query(max_length=2048)] = "/",
) -> BrowseListingDTO:
    """List one folder of a source, to pin a file from it. The source goes
    through the `data_source` resolver: one the caller may not see, another
    tenant's, or none at all is a 404."""
    resolved = await resolve_ref(
        db, viewer=await actor_viewer(db, actor), kind="data_source", ref_id=str(source_id)
    )
    if resolved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found")
    try:
        listing = await browse_source(db, source=resolved[1], path=path)
    except BrowseUnsupported as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, _NOT_BROWSABLE) from exc
    except InvalidReference as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except ConnectorError as exc:
        # Already scrubbed by `browse`: it names the failure, never the
        # connector's own words.
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return BrowseListingDTO(
        entries=[
            BrowseEntryDTO(
                id=e.id,
                name=e.name,
                is_folder=e.is_folder,
                size=e.size,
                content_type=e.content_type,
            )
            for e in listing.entries
        ],
        truncated=listing.truncated,
    )
