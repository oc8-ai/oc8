"""The web UI's own check on record links (spec §5 item 3, ruling R9).

`vet_answer_links` already unlinks what oc8 never issued, but it reads Markdown
with regexes, and the browser reads it with a real parser -- the two can never
be made to agree on every input. So the chat renderer checks the links it is
about to make clickable, exactly as its parser found them: it asks which hosts
are connected systems, and which of its hrefs to those hosts oc8 issued.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from oc8.api.deps import CurrentPrincipal, DbSession, unguarded
from oc8.records.registry import linked_hosts, verified_links
from oc8.records.vetting import connection_hosts
from oc8.schemas.dto import RecordLinkHostsDTO, VerifiedRecordLinksDTO
from oc8.schemas.requests import VerifyRecordLinksRequest

router = APIRouter()

_UNGUARDED_REASON = (
    "tells a signed-in member only which of the links already in front of them "
    "oc8 issued for their own tenant -- the same trust level as reading the chat "
    "answer that carries them"
)


@router.get(
    "/record-links/hosts",
    response_model=RecordLinkHostsDTO,
    dependencies=[Depends(unguarded(_UNGUARDED_REASON))],
)
async def connected_hosts(db: DbSession, principal: CurrentPrincipal) -> RecordLinkHostsDTO:
    """Hosts a record link may point at: every host oc8 issued a link for, and
    the base-URL host of every live connection -- a link guessed before the
    first one was issued still names a connected system."""
    hosts = await linked_hosts(db, tenant_id=principal.tenant_id) | await connection_hosts(db)
    return RecordLinkHostsDTO(hosts=sorted(hosts))


@router.post(
    "/record-links/verify",
    response_model=VerifiedRecordLinksDTO,
    dependencies=[Depends(unguarded(_UNGUARDED_REASON))],
)
async def verify(
    body: VerifyRecordLinksRequest, db: DbSession, principal: CurrentPrincipal
) -> VerifiedRecordLinksDTO:
    """The submitted URLs that oc8 issued within the TTL, spelled as submitted,
    so the renderer can compare its own href strings exactly."""
    issued = await verified_links(db, tenant_id=principal.tenant_id, urls=body.urls)
    return VerifiedRecordLinksDTO(issued=[u for u in dict.fromkeys(body.urls) if u in issued])
