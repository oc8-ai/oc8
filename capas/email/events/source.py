"""The `mail.received` event: core polls the agent's mailbox through this
poller and starts the agent for each new mail (`oc8.events.sources`).

Runs in the core process. The protocol work lives in `mcp_bridge/mailbox.py`,
which is loaded here by its path under a name of its own (`_load_mailbox`):
importing it as `mcp_bridge.mailbox` would leave the generic name
`mcp_bridge` cached in the core process, where another capa's bridge package
of the same name could be handed ours (see `oc8.capas.loader`).

What a poll does is described on `mailbox.poll`; what the agent receives is
`mailbox.render_summary`, which carries the mail id and the attachment ids
the tools take, since the run's stored payload is not shown to the model.
Mail the loop protection skips (`mailbox.skip_reason`) moves the cursor on
without an event.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from oc8.events.sources import MAX_EVENTS_PER_TICK, PolledEvent, PollResult
from oc8.net.outbound import OutboundBlocked, guard_outbound_host

EVENT_TYPE = "mail.received"
CREDENTIAL_TYPE = "mail_account"
_MODULE = "oc8_capa_email_mailbox"


def _load_mailbox() -> ModuleType:
    loaded = sys.modules.get(_MODULE)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parents[1] / "mcp_bridge" / "mailbox.py"
    spec = importlib.util.spec_from_file_location(_MODULE, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[_MODULE]
        raise
    return module


mailbox = _load_mailbox()


def guard(host: str) -> None:
    """Core's outbound rule for an address a person typed: no internal
    address unless the operator allows it (OC8_OUTBOUND_ALLOWED_PRIVATE)."""
    try:
        guard_outbound_host(host)
    except OutboundBlocked:
        raise mailbox.MailError(
            f"The mail server {host} is not reachable from here.", reason="unreachable"
        ) from None


async def poll(
    login_fields: dict[str, str], event_config: dict[str, Any], cursor: dict[str, Any] | None
) -> PollResult:
    account = mailbox.Account.from_fields(login_fields)
    folder = str(event_config.get("folder") or "").strip() or "INBOX"
    next_cursor, fetched = await asyncio.to_thread(
        mailbox.poll, account, folder, cursor, MAX_EVENTS_PER_TICK, guard
    )
    if cursor is None:
        return PollResult(next_cursor=next_cursor)
    events: list[PolledEvent] = []
    for mail in fetched:
        event = mailbox.mail_event(mail, account)
        if event is not None:
            events.append(PolledEvent(**event))
    return PollResult(next_cursor=next_cursor, events=events)


def register(contrib: Any) -> None:
    contrib.add_poller(EVENT_TYPE, poll, credential_type=CREDENTIAL_TYPE)
