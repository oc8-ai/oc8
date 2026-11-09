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
    from oc8.chat.modes import ChatMode
    from oc8.licensing.capabilities import Capability
    from oc8.modelrouter import NeutralTool

logger = logging.getLogger(__name__)

#: At most this many edition parts on one message; the rest are dropped.
MAX_PARTS = 20
#: At most this many bytes of serialised edition parts on one message.
MAX_PARTS_BYTES = 64 * 1024
#: Part types core itself writes; an edition part may not pose as one.
CORE_PART_TYPES: frozenset[str] = frozenset({"answers", "suggestions", "agent_builder"})
#: How deep a part may nest (the part object itself is depth 1).
MAX_PART_DEPTH = 8
_MAX_TYPE_LENGTH = 64
#: Integers beyond this many bits are refused: JSON readers disagree on them.
_MAX_INT_BITS = 63


@dataclass(frozen=True)
class EditionToolContext:
    """What core hands an edition tool's executor for one call.

    `db` is the run's own session, already bound to `tenant_id` (row-level
    security). An executor works through it: it must not open sessions of its
    own, and must not rebind the tenant or move `agent`/`task` to another one.
    What it returns -- `ControlOutcome.output` and `.parts` -- is untrusted:
    the output reaches the model like any tool result, and the parts pass
    `clean_parts` before they are stored.
    """

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


#: Edition tool names whose clash with an agent's own tool was already logged.
_WARNED_CLASHES: set[str] = set()

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
    """The registered edition tool of that name, if any.

    Not authorisation: being registered says nothing about whether this
    tenant is entitled to it or whether it was offered on this run. The
    dispatcher checks that at call time (`edition_tool_entitled`, and that it
    was offered), never relying on this lookup alone.
    """
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
            if name not in _WARNED_CLASHES:
                # Once per process and name: the offer is recomputed on every
                # gateway call, and the clash does not change between them.
                _WARNED_CLASHES.add(name)
                logger.warning("edition tool %r withheld: the agent has a tool of that name", name)
            continue
        offered.append(tool)
    return offered


@dataclass(frozen=True)
class EditionOffer:
    """The edition tools offered on one run, leg or door, computed once by the
    runtime that assembles the tool list and handed to the PEP and the
    dispatcher, so a call is checked against what was actually offered.

    `taken` holds the names the agent's own tools already use (MCP tools,
    advertised and real, and skill tools). A name in it is never the edition
    tool's, whatever the registry says: the agent's tool wins.
    """

    tools: tuple[EditionTool, ...] = ()
    taken: frozenset[str] = frozenset()

    @property
    def names(self) -> frozenset[str]:
        return frozenset(t.tool.name for t in self.tools)

    def neutral_tools(self) -> list[NeutralTool]:
        return [t.tool for t in self.tools]

    def claims(self, name: str) -> bool:
        """Whether a call of this name is an edition-tool call -- offered or
        not. Such a call is decided by the edition gates and nowhere else."""
        return name not in self.taken and registered_edition_tool(name) is not None

    def offered(self, name: str) -> EditionTool | None:
        return next((t for t in self.tools if t.tool.name == name), None)


#: No edition tool offered, nothing taken: every registered name is claimed
#: and refused. The fail-closed default for a caller that passes no offer.
NO_EDITION_OFFER = EditionOffer()


async def edition_offer(
    tenant_id: uuid.UUID,
    *,
    door: str | None,
    taken: Collection[str] = (),
    chat_mode: ChatMode | None = None,
) -> EditionOffer:
    """What `offered_edition_tools` offers this tenant on `door`, narrowed by
    the chat mode: nothing in a tool-less mode, no writing tool in a read-only
    one."""
    taken_names = frozenset(taken)
    if chat_mode is not None and not chat_mode.allows_tools:
        return EditionOffer((), taken_names)
    tools = await offered_edition_tools(tenant_id, door=door, taken=taken_names)
    if chat_mode is not None and not chat_mode.allows_writes:
        tools = [t for t in tools if not t.writes]
    return EditionOffer(tuple(tools), taken_names)


class _Refused(Exception):
    """Why a part was dropped."""


def _check_string(value: str, budget: int) -> int:
    """The string's UTF-8 size; refuses one over `budget`, with a NUL, or with
    a lone surrogate (neither survives JSONB). The length test comes first so
    a huge string is never encoded."""
    if len(value) > budget:
        raise _Refused("too large")
    if "\x00" in value:
        raise _Refused("contains NUL")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise _Refused("not valid UTF-8 (lone surrogate)") from None
    if size > budget:
        raise _Refused("too large")
    return size


def _plain_json(raw: dict[Any, Any], budget: int) -> dict[str, Any]:
    """A plain-JSON copy of `raw`, walked iteratively (no recursion) within
    MAX_PART_DEPTH and an estimated `budget` of bytes, so neither deep nesting
    nor a huge value is ever serialised whole before being refused."""
    used = 0

    def spend(n: int) -> None:
        nonlocal used
        used += n
        if used > budget:
            raise _Refused("too large")

    root: dict[str, Any] = {}
    # (source container, its copy, depth of the container)
    stack: list[tuple[Any, Any, int]] = [(raw, root, 1)]
    while stack:
        src, dst, depth = stack.pop()
        spend(2)  # brackets
        items: list[tuple[str | None, Any]]
        if isinstance(src, dict):
            items = []
            for key in src:
                if not isinstance(key, str):
                    raise _Refused("non-string key")
                spend(_check_string(key, budget - used) + 4)  # quotes, colon, comma
                items.append((key, src[key]))
        else:
            items = [(None, v) for v in src]
        for key, value in items:
            spend(1)
            copy: Any
            if value is None or isinstance(value, bool):
                copy = value
                spend(len(json.dumps(value)))
            elif isinstance(value, int):
                if value.bit_length() > _MAX_INT_BITS:
                    raise _Refused("integer out of range")
                copy = int(value)
                spend(len(str(copy)))
            elif isinstance(value, float):
                if value != value or value in (float("inf"), float("-inf")):
                    raise _Refused("non-finite number")
                copy = float(value)
                spend(len(repr(copy)))
            elif isinstance(value, str):
                spend(_check_string(value, budget - used) + 2)
                copy = str(value)
            elif isinstance(value, dict | list | tuple):
                if depth >= MAX_PART_DEPTH:
                    raise _Refused(f"nested deeper than {MAX_PART_DEPTH}")
                copy = {} if isinstance(value, dict) else []
                stack.append((value, copy, depth + 1))
            else:
                raise _Refused(f"not JSON ({type(value).__name__})")
            if key is None:
                dst.append(copy)
            else:
                dst[key] = copy
    return root


def _clean_part(raw: object, budget: int) -> tuple[dict[str, Any], int]:
    """The part as plain JSON and its serialised size; raises _Refused."""
    if not isinstance(raw, dict):
        raise _Refused("not an object")
    kind = raw.get("type")
    if not isinstance(kind, str) or not kind or len(kind) > _MAX_TYPE_LENGTH:
        raise _Refused("no string type")
    if kind in CORE_PART_TYPES:
        raise _Refused(f"reserved type {kind!r}")
    part = _plain_json(raw, budget)
    # Bounded and validated by the walk, so this is cheap and cannot fail on
    # content; ensure_ascii keeps the measure independent of the encoding.
    size = len(json.dumps(part, allow_nan=False, ensure_ascii=True, separators=(",", ":")))
    if size > budget:
        raise _Refused("too large")
    return part, size


def clean_parts(raw: object) -> list[dict[str, Any]]:
    """Untrusted edition parts made safe to store: JSON objects with a non-empty
    string `type` that is not a core part type, nested at most MAX_PART_DEPTH
    deep, no NUL or lone surrogate in any string, at most MAX_PARTS of them
    and MAX_PARTS_BYTES serialised. Anything else is dropped with a warning.

    Never raises: whatever an edition hands back, the reply is still stored.
    """
    try:
        if not isinstance(raw, list):
            if raw is not None:
                logger.warning("edition parts dropped: expected a list, got %s", type(raw).__name__)
            return []
        out: list[dict[str, Any]] = []
        used = 0
        for index, item in enumerate(raw):
            if len(out) >= MAX_PARTS:
                logger.warning(
                    "edition parts dropped: %d beyond the limit of %d",
                    len(raw) - index,
                    MAX_PARTS,
                )
                break
            try:
                part, size = _clean_part(item, MAX_PARTS_BYTES - used)
            except _Refused as refused:
                logger.warning("edition part %d dropped: %s", index, refused)
                continue
            except Exception:
                logger.warning("edition part %d dropped: could not be read", index, exc_info=True)
                continue
            used += size
            out.append(part)
        return out
    except Exception:
        logger.warning("edition parts dropped: could not be read", exc_info=True)
        return []
