"""Click-through URL for the record a tool call names.

The live log and the task card already say which record is open. This is the
address of that record in the system itself, so the operator can open it.
The core only builds shapes it can form from a base URL the connection already
holds: an Odoo form view, and a Jira issue browse page. A mailbox has no such
address in the credential env, so a mail line stays text.
"""

from __future__ import annotations

import re

_ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


def record_url_from_env(
    system: str, env: dict[str, str], identity: tuple[str, str] | None
) -> str | None:
    """The source-system address of `identity`, or None when this system has none."""
    if identity is None:
        return None
    if system == "odoo":
        return odoo_form_url(str(env.get("ODOO_URL") or ""), identity[0], identity[1])
    if system == "jira":
        return jira_browse_url(str(env.get("JIRA_URL") or ""), identity[1])
    return None


def odoo_form_url(base: str, model: str, ref: str) -> str | None:
    """An Odoo form URL, or None when the pieces are not a real record."""
    root = base.strip().rstrip("/")
    if not root.startswith(("http://", "https://")):
        return None
    kind = model.strip()
    record_id = ref.strip().lstrip("#")
    if not kind or not record_id.isdigit():
        return None
    return f"{root}/web#id={record_id}&model={kind}&view_type=form"


def jira_browse_url(base: str, ref: str) -> str | None:
    """A Jira browse URL for an issue key, or None for a search or a bare host."""
    root = base.strip().rstrip("/")
    if not root.startswith(("http://", "https://")):
        return None
    key = ref.strip()
    if not _ISSUE_KEY.match(key):
        return None
    return f"{root}/browse/{key}"
