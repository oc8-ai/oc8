"""The connection test for a `mail_account` login: log in to the incoming
server (IMAP or POP3) and to SMTP, and send nothing.

Runs in the core process, under core's outbound rule for addresses a person
typed (`events.source.guard` does the same for the poller). The protocol work
is `mcp_bridge/mailbox.py`, loaded by path under a name of its own for the
reason `events/source.py` gives.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from oc8.credentials.errors import CredentialCheckFailed
from oc8.net.outbound import OutboundBlocked, guard_outbound_host

_MODULE = "oc8_capa_email_mailbox"


def _load_mailbox() -> ModuleType:
    loaded = sys.modules.get(_MODULE)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parent / "mcp_bridge" / "mailbox.py"
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


async def validate_mail_account(values: dict[str, str]) -> None:
    mailbox = _load_mailbox()

    def guard(host: str) -> None:
        try:
            guard_outbound_host(host)
        except OutboundBlocked:
            raise mailbox.MailError(
                f"The mail server {host} is not reachable from here.", reason="unreachable"
            ) from None

    try:
        account = mailbox.Account.from_fields(values)
        await asyncio.to_thread(mailbox.check_login, account, guard=guard)
    except mailbox.MailError as exc:
        raise CredentialCheckFailed(str(exc), reason=exc.reason) from None
