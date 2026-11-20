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

from collections.abc import Sequence


class MailboxRefused(RuntimeError):
    """A mailbox the login may not act as, in words safe to show."""


def mailbox_list(raw: str) -> list[str]:
    """The comma-joined list the core keeps the delegated mailboxes in."""
    return [m.strip() for m in raw.split(",") if m.strip()]


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
        if mailbox and mailbox.casefold() != personal.casefold():
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
    if target.casefold() not in {m.casefold() for m in delegated}:
        raise MailboxRefused(
            f"mailbox {target!r} is not one of this connection's authorized delegated "
            f"mailboxes ({', '.join(delegated) or 'none configured'})"
        )
    return target
