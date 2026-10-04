"""Turning a tool pack's `record_url` declaration into a link an approver can
click (§6 of the AI workplace design).

Core knows three placeholders and nothing else. It does not know that Odoo
exists, that `sale.order` is a quotation, or that a record id is an integer --
the pack says where the base URL lives in its own connection config, which of
its entities are linkable, and what the path looks like.

EVERY failure is `None`. This runs inside `mcp_gateway._call_tool` and
`internal_agent`'s tool endpoint, microseconds before a human gets asked a
question they may have been waiting for; a cosmetic link that raised would
turn a governed pause into a failed run. That is also why the "absence
degrades to today's behaviour" rule from the design is expressed as a return
value here rather than as a caller-side branch.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

from oc8 import models as m
from oc8.capas.discovery import resolve_tool_pack_connection
from oc8.capas.manifest import RecordUrlTemplate

logger = logging.getLogger(__name__)

#: Long enough for any real record URL, short enough that a pathological tool
#: argument cannot push a multi-kilobyte string into a Telegram message or an
#: `href`. Dropped rather than truncated -- half a URL is worse than none.
_MAX_URL_CHARS = 2000


def _walk(config: Mapping[str, Any], path: Sequence[str]) -> str:
    """The value at `path` inside a connection's config, as a string.

    Same "a path is a list of keys" convention `value_spec.line_items.path`
    already uses. Anything missing, or a non-leaf where a leaf was expected,
    is the empty string -- a connection an operator has not finished setting
    up simply has no link yet.
    """
    node: Any = config
    for key in path:
        if not isinstance(node, Mapping):
            return ""
        node = node.get(key)
    return str(node).strip() if isinstance(node, str) else ""


def resolve_record_url(
    spec: RecordUrlTemplate | None,
    *,
    config: Mapping[str, Any],
    entity: str,
    ref: str,
) -> str | None:
    """The record's URL, or None -- pure, and never raises.

    `entity`/`ref` are exactly the pair `agent.tool_semantics.record_identity`
    already produces for a call that names ONE record; a search names a kind of
    record rather than one, gets no identity there, and therefore gets no link
    here either.
    """
    if spec is None or not entity or not ref:
        return None
    segment = spec.models.get(entity)
    if not segment:
        # Only listed entities are linkable -- the same rule `focus_spec.labels`
        # uses for the live log. Silent on purpose: an agent touching an
        # unlisted model is ordinary, not an error.
        return None
    base = _walk(config, spec.base_url_path)
    if not base:
        return None
    try:
        url = spec.template.format(
            base_url=base.rstrip("/"),
            # Both are percent-encoded: a record reference comes out of a tool
            # CALL's arguments, i.e. out of a model's output, and this string
            # lands in an `href` and in a chat message. `safe=""` so a slash in
            # a ref cannot climb out of its path segment.
            model=quote(segment, safe=""),
            id=quote(ref, safe=""),
        )
    except (KeyError, IndexError, ValueError):
        # Unreachable for a manifest that passed `RecordUrlTemplate`'s own
        # validator, which is exactly why this is a `return None` and not a
        # raise: a hand-written row or a future template shape must not take a
        # tool call down.
        logger.warning("record_url template %r could not be formatted", spec.template)
        return None
    if not url.startswith(("http://", "https://")):
        logger.warning("record_url for %s resolved to a non-http URL; dropping it", entity)
        return None
    if len(url) > _MAX_URL_CHARS:
        logger.warning("record_url for %s resolved to %d characters; dropping it", entity, len(url))
        return None
    return url


def record_url_for_connection(
    conn: m.McpConnection | None, *, entity: str, ref: str
) -> str | None:
    """The same answer for a LIVE connection row.

    The template is read off the plugin manifest on disk, never off the row --
    `resolve_tool_pack_connection`'s own docstring explains why plugin-authored
    declarations live there. One consequence worth knowing: editing a pack's
    `tool_pack.toml` changes this within the discovery cache's TTL, with no
    re-install and no re-enable, unlike the `config` seams
    `materialise._refresh_declared_seams` has to copy across.
    """
    if conn is None:
        return None
    cfg = conn.config if isinstance(conn.config, dict) else {}
    manifest_conn = resolve_tool_pack_connection(
        str(cfg.get("_plugin_name", "")), str(cfg.get("_connection_key", ""))
    )
    if manifest_conn is None:
        return None
    return resolve_record_url(manifest_conn.record_url, config=cfg, entity=entity, ref=ref)
