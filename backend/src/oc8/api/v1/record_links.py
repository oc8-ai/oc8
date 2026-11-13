"""The web UI's own check on the links in a chat answer (spec §5 item 3,
rulings R9, R11 and R15).

`vet_answer_links` already unlinks what oc8 never issued, but it reads Markdown
with regexes, and the browser reads it with a real parser -- the two can never
be made to agree on every input. So the chat renderer checks the hrefs its
parser actually produced, for one saved answer at a time.

The check is bound to that answer on purpose. A free-standing "did oc8 issue
this URL?" would let any member probe constructed URLs and learn which records
any agent touched for anyone in the tenant. Here the caller must be able to
read the message, and only URLs that the message itself contains are judged;
the list of connected hosts never leaves the server, and no host the caller
names is ever looked up. Each verdict carries the server's reading of the
URL's host, so the client keeps a link as text where its own reading differs.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from oc8 import models as m
from oc8.api.deps import DbSession, require_departmental
from oc8.api.v1.chat import _readable_session
from oc8.authz.permissions import AGENT, COPILOT_USE, VIEW, perm
from oc8.authz.scope import HumanActor
from oc8.records.registry import canonical, host_of, linked_hosts, verified_links
from oc8.records.vetting import connection_hosts, urls_in
from oc8.schemas.dto import RecordLinkVerdictDTO, RecordLinkVerdictsDTO
from oc8.schemas.requests import VerifyRecordLinksRequest

router = APIRouter()


@router.post("/record-links/verify", response_model=RecordLinkVerdictsDTO)
async def verify(
    request: Request,
    body: VerifyRecordLinksRequest,
    db: DbSession,
    # The same door as reading the chat's messages (`api/v1/chat.py`).
    actor: Annotated[
        HumanActor,
        Depends(require_departmental(perm(AGENT, VIEW), or_tenant_wide=COPILOT_USE)),
    ],
) -> RecordLinkVerdictsDTO:
    """For each submitted URL: does it point at a connected system, and did oc8
    issue it within the TTL? Only an assistant answer is checked -- a member's
    own message would let them choose the URLs -- and a URL is judged only if
    the answer contains it whole (spelled any way that leads to the same
    place, as the browser re-encodes hrefs); any other is reported connected
    and not issued, which tells nothing and renders as text. 404 for an
    answer the caller may not read, the same as one that does not exist."""
    msg = await db.get(m.ChatMessage, body.message_id)
    if msg is None or msg.role != "assistant":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "message not found")
    await _readable_session(request, msg.session_id, db, actor)

    tenant_id = actor.principal.tenant_id
    urls = list(dict.fromkeys(body.urls))
    # In the answer first: only a URL the answer itself holds -- whole, by
    # where it leads -- is judged at all. Any other gets the same fail-closed
    # verdict whatever its host, or the endpoint would say which hosts are
    # connected to anyone who can read one answer.
    mentioned = {c for found in urls_in(msg.content) if (c := canonical(found)) is not None}
    in_answer = {u: host_of(u) for u in urls if canonical(u) in mentioned}
    hosts: set[str] = set()
    if in_answer:
        hosts = await linked_hosts(db, tenant_id=tenant_id) | await connection_hosts(db)
        hosts.discard("")
    # Only our own reading of a host decides; the client compares it with the
    # host it will open and keeps the link as text where the two disagree.
    connected = {u for u, host in in_answer.items() if host in hosts}
    issued = await verified_links(db, tenant_id=tenant_id, urls=connected)
    return RecordLinkVerdictsDTO(
        links=[
            RecordLinkVerdictDTO(
                url=u, host=in_answer[u], connected=u in connected, issued=u in issued
            )
            if u in in_answer
            else RecordLinkVerdictDTO(url=u, host="", connected=True, issued=False)
            for u in urls
        ]
    )
