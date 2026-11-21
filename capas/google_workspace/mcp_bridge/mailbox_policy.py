"""Which mailbox a Google login may act as: the one rule for the tools
(`google_api.resolve_mailbox_address`, in the bridge process) and the
`mail.received` poller (`events/source.py`, in the core process, which loads
this file by path). Pure: no environment, no network, no other import.

* A person's own sign-in (`authorization_code`) acts as that account only.
* Otherwise the mailbox must be one the setup authorized for domain-wide
  delegation (`delegated_mailboxes`), compared case-insensitively; an empty
  list authorizes nobody.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

#: Commas, semicolons and whitespace (newlines included): the same split as
#: core's `oc8.events.sources.setup_list`, which checks the trigger setting.
_SEPARATORS = re.compile(r"[\s,;]+")
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


class MailboxRefused(RuntimeError):
    """A mailbox the login may not act as, in words safe to show."""


def mailbox_list(raw: str) -> list[str]:
    """The list the core keeps the delegated mailboxes in."""
    return [m for m in _SEPARATORS.split(raw) if m]


def same_address(a: str, b: str) -> bool:
    """Equal but for ASCII case: no Unicode folding, so two different
    addresses (STRAßE, strasse) never compare equal."""
    return a.translate(_ASCII_LOWER) == b.translate(_ASCII_LOWER)


def authorized_mailbox(
    mailbox: str | None,
    *,
    default: str = "",
    delegated: Sequence[str],
    personal: str | None,
) -> str:
    """The address to act as: `mailbox`, else `default`. Returns the
    original casing, never the casefolded form -- Gmail and Calendar URLs
    and attendee matching want the real address."""
    if personal is not None:
        if mailbox and not same_address(mailbox, personal):
            raise MailboxRefused(
                f"this connection acts as {personal} only; omit mailbox or pass {personal}"
            )
        return personal
    target = mailbox or default
    if not target:
        raise MailboxRefused(
            "no mailbox given and no default_mailbox configured for this connection -- "
            "pass mailbox, or set a default mailbox in the plugin's setup form"
        )
    if not any(same_address(target, m) for m in delegated):
        raise MailboxRefused(
            f"mailbox {target!r} is not one of this connection's authorized delegated "
            f"mailboxes ({', '.join(delegated) or 'none configured'})"
        )
    return target
