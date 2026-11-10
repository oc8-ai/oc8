"""Authenticated per-tenant WebSocket endpoint. The token (query param, since
browsers can't set WS headers) is verified; the connection joins ONLY its own
tenant's fan-out set."""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, WebSocket, status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from oc8.agents.repo import reads_every_run
from oc8.auth.provider import InvalidToken, get_identity_provider
from oc8.authz.authority import resolve_authority
from oc8.authz.permissions import RUN, VIEW, perm
from oc8.chat.watch import may_watch_run
from oc8.db.session import tenant_session
from oc8.models import OrgMember
from oc8.realtime.manager import ConnectionManager, Recheck, WatchVerdict

logger = logging.getLogger(__name__)

router = APIRouter()

#: The socket carries the ACTIVITY FEED, live, as free text: `activity.logged`
#: ships `message` and `detail` verbatim, and among its producers are
#: `control_tools.py` ("Entscheidung angefragt: {question}", with `context[:500]`
#: as detail -- the very question `GET /clarifications` now scopes),
#: `agent/engine.py` ("Operator: {msg}", "{agent} completed: {task_text}") and
#: `note_focus` (what an agent is working on right now).
#:
#: `GET /activity`, which serves the same rows over HTTP, has always been
#: `run:view`. The socket asked for nothing but a valid token, which cost nothing
#: while every account in the system was an administrator -- and this slice is
#: what creates a population that is NOT: a `member` holds `frozenset()` and needs
#: no seat at all, so he 403'd at `/activity` and was simultaneously joined to the
#: tenant-wide fan-out carrying its contents. Same rows, same permission.
#:
#: Not per-department rooms: that is §10 item 5 and slice 2. The fan-out is the
#: WHOLE tenant's, unfiltered, while `GET /activity` now narrows to the agents
#: whose runs the caller may see (`agents.repo.run_reach`). The socket cannot
#: narrow, so it admits only the caller for whom that narrowing is a no-op: a
#: tenant-wide read of BOTH `agent:view` and `run:view`
#: (`agents.repo.reads_every_run`, i.e. `reach.every_agent`). Holding
#: `run:view` alone is not enough -- that caller's HTTP feed is departmental,
#: and a tenant-wide socket would hand him every other department's activity.
_FEED_PERMISSION = perm(RUN, VIEW)


@router.websocket("/events/ws")
async def events_ws(websocket: WebSocket) -> None:
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    try:
        principal = get_identity_provider().verify(token)
    except InvalidToken:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    # This feed is the operator's view of a tenant. An agent token is minted for a
    # container and belongs to the two gateways, not here -- subscribing with one
    # would let a container watch every run, approval and activity in its tenant.
    # Checked in place rather than as a router dependency: `deny_agent_principals`
    # is an HTTP dependency and a WebSocket route cannot carry one.
    if principal.kind == "agent":
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    # A `totp:enroll`/`totp:challenge`-scoped token (standalone 2FA design)
    # proves a password at most -- the second factor is still outstanding --
    # so it must not be able to open this feed and stream the tenant's entire
    # live event stream (every run, approval, clarification, activity event)
    # for as long as the socket stays open. The operator API refuses these
    # tokens through `deny_totp_pending_principals`, a router dependency; a
    # WebSocket cannot carry one, so it is refused here in place, same reason
    # and same shape as the agent-kind check just above.
    if "totp:enroll" in principal.scopes or "totp:challenge" in principal.scopes:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    # And the same permission the HTTP route serving these rows declares --
    # resolved the same way too, which is the half that is easy to skip. A
    # WebSocket route cannot carry an HTTP dependency (the same reason the agent
    # check above is written in place rather than at the router), so the session
    # is opened here by hand rather than the answer being read off the token: an
    # administrator demoted to a tenant-defined role holds `run:view` nowhere,
    # and a socket that kept asking the code table would have gone on streaming
    # him every run, approval and clarification in the company for as long as his
    # tab stayed open. Admitted only as an every-agent run reader (see
    # `_FEED_PERMISSION`): anybody narrower is narrowed over HTTP and cannot be
    # here.
    #
    # Fail closed on a database error: this feed carries the same rows
    # `GET /activity` refuses, and during a blip that route answers nothing
    # either.
    try:
        async with tenant_session(principal.tenant_id) as db:
            authority = await resolve_authority(db, principal)
            member = (
                await db.execute(
                    select(OrgMember).where(
                        OrgMember.tenant_id == principal.tenant_id,
                        OrgMember.subject == principal.subject,
                        OrgMember.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            member_id = member.id if member is not None else None
    except SQLAlchemyError:
        logger.exception("could not resolve the caller's authority for the realtime feed")
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return
    # The tenant-wide feed only for an every-agent run reader (see
    # `_FEED_PERMISSION`): anybody narrower is narrowed over HTTP.
    feed = reads_every_run(authority, _FEED_PERMISSION)
    # A member-only socket (spec §2.4) carries nothing but this member's own
    # events (`audience` envelopes) and the runs they were allowed to watch --
    # never the feed. Without a member row there is nothing it could carry, so
    # the old refusal stands.
    if not feed and member_id is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    manager: ConnectionManager = websocket.app.state.realtime_manager
    tenant_id = principal.tenant_id
    await websocket.accept()
    recheck = (
        _watch_recheck(tenant_id, member_id, principal.role) if member_id is not None else None
    )
    await manager.connect(tenant_id, websocket, member_id=member_id, feed=feed, recheck=recheck)
    # Handshake: connect() only returns once the tenant subscription is live,
    # so this genuinely means events are now being received. The client uses
    # it to (re)sync, and `feed` to know whether it gets the tenant feed.
    await websocket.send_json({"type": "realtime.connected", "feed": feed})
    try:
        while True:
            raw = await websocket.receive_text()
            await _handle_client_message(
                websocket, manager, tenant_id, member_id, principal.role, raw
            )
    except Exception:
        pass
    finally:
        await manager.disconnect(tenant_id, websocket)


def _watch_recheck(tenant_id: uuid.UUID, member_id: uuid.UUID, token_role: str | None) -> Recheck:
    """The periodic re-authorisation of this socket's run watches: the same
    `may_watch_run` that granted them, re-read from the database, so a revoked
    seat, a changed role or an offboarded member stops the stream."""

    async def _recheck(run_id: str) -> WatchVerdict:
        async with tenant_session(tenant_id) as db:
            member = await db.get(OrgMember, member_id)
            if member is None or member.deleted_at is not None:
                return "gone"
            allowed = await may_watch_run(
                db, member=member, run_id=uuid.UUID(run_id), token_role=token_role
            )
        return "ok" if allowed else "access"

    return _recheck


async def _handle_client_message(
    websocket: WebSocket,
    manager: ConnectionManager,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID | None,
    token_role: str | None,
    raw: str,
) -> None:
    """`{"type": "run.watch", "runId": ...}` is the only client message. An
    unknown or unauthorised run is ignored silently -- an id that answers
    differently is an id somebody can test. Authorisation is re-read from the
    database on every request (`may_watch_run`), so a member who lost the
    run's agent cannot start watching it."""
    try:
        msg = json.loads(raw)
        if not isinstance(msg, dict) or msg.get("type") != "run.watch":
            return
        run_id = uuid.UUID(str(msg.get("runId")))
    except (ValueError, TypeError):
        return
    if member_id is None:
        return
    try:
        async with tenant_session(tenant_id) as db:
            member = await db.get(OrgMember, member_id)
            allowed = member is not None and await may_watch_run(
                db, member=member, run_id=run_id, token_role=token_role
            )
    except SQLAlchemyError:
        logger.warning("could not decide a run.watch request", exc_info=True)
        return
    if allowed:
        manager.watch_run(tenant_id, websocket, str(run_id))
        await websocket.send_json({"type": "run.watching", "runId": str(run_id)})
