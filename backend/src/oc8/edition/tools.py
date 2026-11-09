"""Edition tools (oc8 Chat spec §6.2): tools an edition adds to the agent and
chat tool assembly.

Core owns the seam and every gate around it -- entitlement, mode, PEP, audit;
the edition owns only the executor. Context-local, like the supervision hook,
so one process never leaks one deployment's tools into another's composition.

What an edition tool hands back in `ControlOutcome.parts` is untrusted,
extension-produced data. `clean_parts` is the one gate it passes before it is
stored on a chat message: JSON objects with a string `type`, bounded in count
and size. Core stores and delivers them unchanged and never renders or runs
them -- that is the frontend slots' job.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Collection, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from oc8.licensing.contracts import CommunityEntitlements, Entitlements

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from oc8 import models as m
    from oc8.agent.control_tools import ControlOutcome
    from oc8.licensing.capabilities import Capability
    from oc8.modelrouter import NeutralTool

logger = logging.getLogger(__name__)

#: At most this many edition parts on one message; the rest are dropped.
MAX_PARTS = 20
#: At most this many bytes of serialised edition parts on one message.
MAX_PARTS_BYTES = 64 * 1024
#: Part types core itself writes; an edition part may not pose as one.
CORE_PART_TYPES: frozenset[str] = frozenset({"answers", "suggestions"})
_MAX_TYPE_LENGTH = 64


@dataclass(frozen=True)
class EditionToolContext:
    db: AsyncSession
    tenant_id: uuid.UUID
    agent: m.Agent
    task: m.Task
    run_id: uuid.UUID | None
    arguments: Mapping[str, Any]
    originating_operator: str | None
    pinned: Mapping[str, Any] | None


@dataclass(frozen=True)
class EditionTool:
    tool: NeutralTool
    execute: Callable[[EditionToolContext], Awaitable[ControlOutcome]]
    capability: Capability
    #: True withholds it in read-only modes (/plan, /ask, /summarise).
    writes: bool
    #: Doors (`web`, `telegram`, `followup`, ...) it may be offered on; None = all.
    door_ok: frozenset[str] | None = None


type TenantEntitlements = Callable[[uuid.UUID], Awaitable[Entitlements]]


async def community_entitlements(_tenant_id: uuid.UUID) -> Entitlements:
    """No tenant holds an Enterprise capability in Community."""
    return CommunityEntitlements()


@dataclass(frozen=True)
class _State:
    tools: Mapping[str, EditionTool]
    entitlements: TenantEntitlements


_NO_TOOLS = _State(MappingProxyType({}), community_entitlements)
_STATE: ContextVar[_State] = ContextVar("oc8_edition_tools", default=_NO_TOOLS)


def index_edition_tools(tools: Sequence[EditionTool]) -> dict[str, EditionTool]:
    """Edition tools by name, refusing any that would shadow a core control
    tool or each other. Called when a composition is built, so a clash stops
    the process at startup instead of surfacing in some later run."""
    from oc8.agent.control_tools import CONTROL_TOOL_NAMES

    by_name: dict[str, EditionTool] = {}
    for tool in tools:
        name = tool.tool.name
        if name in CONTROL_TOOL_NAMES:
            raise ValueError(f"edition tool {name!r} would shadow a control tool")
        if name in by_name:
            raise ValueError(f"duplicate edition tool {name!r}")
        by_name[name] = tool
    return by_name


@contextmanager
def _activate(tools: Sequence[EditionTool], entitlements: TenantEntitlements) -> Iterator[None]:
    token = _STATE.set(_State(MappingProxyType(index_edition_tools(tools)), entitlements))
    try:
        yield
    finally:
        _STATE.reset(token)


def use_edition_tools(
    tools: Sequence[EditionTool], entitlements: TenantEntitlements
) -> AbstractContextManager[None]:
    """Make `tools` the registered edition tools for the enclosed scope, gated
    per tenant by `entitlements`. Raises ValueError on a name clash."""
    return _activate(tools, entitlements)


def registered_edition_tool(name: str) -> EditionTool | None:
    return _STATE.get().tools.get(name)


def edition_tool_writes(name: str) -> bool | None:
    tool = registered_edition_tool(name)
    return tool.writes if tool is not None else None


async def edition_tool_entitled(tenant_id: uuid.UUID, tool: EditionTool) -> bool:
    entitlements = await _STATE.get().entitlements(tenant_id)
    return entitlements.has(tool.capability)


async def offered_edition_tools(
    tenant_id: uuid.UUID, *, door: str | None, taken: Collection[str] = ()
) -> list[EditionTool]:
    """The registered edition tools this tenant is entitled to on `door`.

    `taken` names the tools the caller already offers (the agent's MCP tools);
    an edition tool of the same name is withheld so it never shadows one. A
    tool restricted to some doors is not offered when the door is unknown.
    """
    state = _STATE.get()
    if not state.tools:
        return []
    entitlements = await state.entitlements(tenant_id)
    offered: list[EditionTool] = []
    for name, tool in state.tools.items():
        if not entitlements.has(tool.capability):
            continue
        if tool.door_ok is not None and (door is None or door not in tool.door_ok):
            continue
        if name in taken:
            logger.warning("edition tool %r withheld: the agent has a tool of that name", name)
            continue
        offered.append(tool)
    return offered


def _clean_part(raw: object) -> tuple[dict[str, Any], int] | str:
    """The part as plain JSON and its serialised size, or why it was refused."""
    if not isinstance(raw, dict):
        return "not an object"
    kind = raw.get("type")
    if not isinstance(kind, str) or not kind or len(kind) > _MAX_TYPE_LENGTH:
        return "no string type"
    if kind in CORE_PART_TYPES:
        return f"reserved type {kind!r}"
    try:
        encoded = json.dumps(raw, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return "not JSON-serialisable"
    # Round-tripped, so what is stored is plain JSON (no tuples, no subclasses).
    return json.loads(encoded), len(encoded.encode())


def clean_parts(raw: object) -> list[dict[str, Any]]:
    """Untrusted edition parts made safe to store: JSON objects with a non-empty
    string `type` that is not a core part type, at most MAX_PARTS of them and
    MAX_PARTS_BYTES serialised. Anything else is dropped with a warning."""
    if not isinstance(raw, list):
        if raw is not None:
            logger.warning("edition parts dropped: expected a list, got %s", type(raw).__name__)
        return []
    out: list[dict[str, Any]] = []
    used = 0
    for index, item in enumerate(raw):
        if len(out) >= MAX_PARTS:
            logger.warning(
                "edition parts dropped: %d beyond the limit of %d", len(raw) - index, MAX_PARTS
            )
            break
        cleaned = _clean_part(item)
        if isinstance(cleaned, str):
            logger.warning("edition part %d dropped: %s", index, cleaned)
            continue
        part, size = cleaned
        if used + size > MAX_PARTS_BYTES:
            logger.warning(
                "edition part %d dropped: %d bytes would exceed %d", index, size, MAX_PARTS_BYTES
            )
            continue
        used += size
        out.append(part)
    return out
