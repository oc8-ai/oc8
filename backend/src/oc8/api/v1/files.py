"""File attachments: upload endpoints scoped by owner (chat session or agent
instructions), plus the shared GET/DELETE by attachment id. See
docs/superpowers/specs/2026-09-03-chat-and-instruction-file-attachments-design.md's
Security Considerations -- no presigned URLs, every byte flows through here
after the same ownership/RLS checks every other protected resource uses."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from fastapi.responses import Response

from oc8 import models as m
from oc8.agents.repo import visible_agent
from oc8.api.deps import DbSession, require_departmental
from oc8.api.v1.chat import _owned_session
from oc8.authz.authority import authority_for_principal, tenant_wide_read
from oc8.authz.permissions import AGENT, VIEW, perm
from oc8.authz.scope import HumanActor
from oc8.schemas.dto import FileAttachmentDTO
from oc8.storage import s3
from oc8.storage.attachments import (
    AttachmentTooLarge,
    UnsupportedContentType,
    store_attachment_bytes,
)

router = APIRouter()


def _attachment_dto(row: m.FileAttachment) -> FileAttachmentDTO:
    return FileAttachmentDTO(
        id=str(row.id),
        filename=row.filename,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        is_image=row.is_image,
        created_at=row.created_at.isoformat(),
    )


async def _store_upload(
    db: DbSession, *, tenant_id: uuid.UUID, owner_type: str, owner_id: uuid.UUID, file: UploadFile
) -> m.FileAttachment:
    raw = await file.read()
    try:
        return await store_attachment_bytes(
            db,
            tenant_id=tenant_id,
            owner_type=owner_type,
            owner_id=owner_id,
            filename=file.filename or "upload",
            raw=raw,
            content_type=file.content_type or "application/octet-stream",
        )
    except UnsupportedContentType as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except AttachmentTooLarge as exc:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(exc)) from exc


@router.post(
    "/chat/sessions/{session_id}/attachments",
    response_model=FileAttachmentDTO,
    status_code=status.HTTP_201_CREATED,
)
async def upload_chat_attachment(
    request: Request,
    session_id: uuid.UUID,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_departmental(perm(AGENT, VIEW)))],
    file: UploadFile,
) -> FileAttachmentDTO:
    await _owned_session(request, session_id, db, actor)
    row = await _store_upload(
        db,
        tenant_id=actor.principal.tenant_id,
        owner_type="chat_message",
        owner_id=session_id,
        file=file,
    )
    await db.commit()
    return _attachment_dto(row)


async def _owned_attachment(
    request: Request, db: DbSession, actor: HumanActor, attachment_id: uuid.UUID
) -> m.FileAttachment:
    """Same ownership contract every other protected read enforces: either the
    row does not exist, or it exists but this caller may not see it -- both
    404 identically, so a caller can never learn which is true.

    The `chat_message` branch has two shapes because `FileAttachment.owner_id`
    changes meaning partway through a turn's life (see the design doc's Data
    Model section): it is a `ChatSession.id` from the moment a file is
    uploaded until the message that references it is actually sent, and a
    `ChatMessage.id` from `chat/service.py`'s `send_message` re-point onward.
    Try the session lookup directly first (the pre-send shape, and the
    overwhelmingly common one -- most attachments are read back moments after
    upload, before the surrounding message exists at all); only fall back to
    resolving through the message once that comes back empty, then re-run the
    exact same session-ownership check against the message's session. Either
    branch failing -- session/message missing, or found but not owned by this
    actor -- is the same 404 as above.

    `agent_run` (a file an agent produced during a run -- see
    `oc8.runtime.workspace`/`write_output_file`) shares the `agent_instructions`
    visibility check: `owner_id` is the `AgentRun.id`, resolved to its
    `agent_id` first, then gated the same way.
    """
    row = await db.get(m.FileAttachment, attachment_id)
    if row is None or row.tenant_id != actor.principal.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
    if row.owner_type in ("agent_instructions", "agent_run"):
        if row.owner_type == "agent_run":
            run = await db.get(m.AgentRun, row.owner_id)
            if run is None or run.tenant_id != actor.principal.tenant_id:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
            agent_id = run.agent_id
        else:
            agent_id = row.owner_id
        authority = await authority_for_principal(request, db, actor.principal)
        tenant_wide = tenant_wide_read(authority, perm(AGENT, VIEW))
        agent = await visible_agent(
            db, scope=actor.scope, tenant_wide=tenant_wide, agent_id=agent_id
        )
        if agent is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
        return row

    from oc8.chat.service import get_session

    session = await get_session(db, tenant_id=actor.principal.tenant_id, session_id=row.owner_id)
    if session is None:
        message = await db.get(m.ChatMessage, row.owner_id)
        if message is None or message.tenant_id != actor.principal.tenant_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
        session = await get_session(
            db, tenant_id=actor.principal.tenant_id, session_id=message.session_id
        )
    if session is None or session.member_id != actor.member.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
    return row


@router.get("/files/{attachment_id}")
async def download_file(
    attachment_id: uuid.UUID,
    request: Request,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_departmental(perm(AGENT, VIEW)))],
) -> Response:
    row = await _owned_attachment(request, db, actor, attachment_id)
    raw = await s3.get_object(row.bucket_key)
    return Response(
        content=raw,
        media_type=row.content_type,
        headers={
            # `content_type` is the CLIENT-declared MIME type (the allowlist
            # checks it, nothing re-sniffs the bytes -- a deliberate scoping
            # decision, see the design doc's Extraction Pipeline section), and
            # `text/html` is on that allowlist. Caddy puts the API and the SSR
            # frontend on ONE origin, so serving stored bytes inline would let
            # an uploaded .html run as a same-origin document. Forced to a
            # download, and told not to sniff -- the same shape every other
            # file-serving endpoint here already uses (backup.py, audit.py,
            # capas.py).
            "Content-Disposition": "attachment",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/files/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(
    attachment_id: uuid.UUID,
    request: Request,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_departmental(perm(AGENT, VIEW)))],
) -> None:
    row = await _owned_attachment(request, db, actor, attachment_id)
    await s3.delete_object(row.bucket_key)
    await db.delete(row)
    await db.commit()
