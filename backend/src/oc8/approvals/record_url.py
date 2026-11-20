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

Thin wrappers over `oc8.records.links`, which holds the resolution itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from oc8 import models as m
from oc8.capas.manifest import RecordUrlTemplate
from oc8.records.links import link_context, link_context_from, link_for


def resolve_record_url(
    spec: RecordUrlTemplate | None,
    *,
    config: Mapping[str, Any],
    entity: str,
    ref: str,
) -> str | None:
    """The record's URL, or None -- pure, and never raises."""
    return link_for(link_context_from(spec, config=config), entity=entity, ref=ref)


def record_url_for_connection(conn: m.McpConnection | None, *, entity: str, ref: str) -> str | None:
    """The same answer for a LIVE connection row."""
    return link_for(link_context(conn), entity=entity, ref=ref)
