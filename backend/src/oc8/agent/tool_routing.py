"""Which connection a tool call belongs to, when an agent has more than one.

Until now a run reached exactly one connected system: the runtime carried a
single `mcp_connection_id`, and where none was set the core took the department's
oldest connected one -- literally `.limit(1)`. An agent with two systems set up
saw one of them, silently.

Working across two systems is the point of the thing. A support agent that reads
a ticket in one system and opens a development issue in another is not doing
twice as much of the same work; it is carrying an identifier across a boundary,
which is exactly what no single-tool agent can ever demonstrate.

That makes tool NAMES ambiguous for the first time. Two servers may both offer
`create_issue`, and a model calling the bare name would reach whichever the core
happened to look at first -- a silent wrong-system write, the worst failure this
codebase can produce. So:

* a name offered by exactly ONE connection stays bare, because every existing
  mission, skill and plugin says `post_message`, and renaming those wholesale to
  fix a collision that does not exist would break all of them;
* a name offered by SEVERAL is advertised once per connection as
  `<connection>__<tool>`, and the bare form is not offered at all. The model
  cannot express the ambiguous call, so it cannot make it.

Every advertised name is one every model provider accepts,
`^[a-zA-Z0-9_-]{1,64}$`: a dot (the separator until 0.22.1) or a space in a
connection name is refused outright by Anthropic, OpenAI and the gateways in
front of them. Characters outside that set become `_`, a name past 64 is cut
and given a short hash, and where two names would still meet the later one
(in a fixed order) gets a hash too, so no call can reach the neighbour.

The core still names no software. It only knows that connections have names and
that tools have names.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from oc8.modelrouter.types import NeutralTool

#: Separates a connection from a tool in an advertised name. Not a dot: no
#: model provider takes one in a tool name.
SEPARATOR = "__"

#: The dot runs started before 0.22.1 may still call with.
_LEGACY_SEPARATOR = "."

#: The longest tool name every provider takes (OpenAI's cap; Anthropic's is 128).
MAX_NAME = 64

_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")
_HASH_CHARS = 6


@dataclass(frozen=True)
class Route:
    """Where an advertised tool name actually goes."""

    connection: str
    tool: str


def build_routes(
    offered: dict[str, list[str]], *, reserved: Collection[str] = frozenset()
) -> dict[str, Route]:
    """Advertised name -> route, given each connection's own tool names.

    `offered` maps connection name to the tools that connection exposes. Order
    is irrelevant: the result depends only on which names collide, so two runs
    over the same connections always advertise the same names.

    `reserved` names are always qualified, even when only one connection has
    them: the core's own tools. A bare core name then can only ever mean the
    core tool, so no runtime has to guess which of two same-named tools a call
    meant, and no connection tool is decided as the core one.
    """
    owners: dict[str, list[str]] = defaultdict(list)
    for connection, tools in offered.items():
        for tool in tools:
            owners[tool].append(connection)

    bare: list[Route] = []
    qualify: list[Route] = []
    for tool, connections in owners.items():
        if len(connections) == 1 and tool not in reserved:
            bare.append(Route(connections[0], tool))
        else:
            qualify.extend(Route(connection, tool) for connection in connections)

    # Bare names first: they are what missions and skills already say. Each
    # group in a fixed order, so every run advertises the same names.
    routes: dict[str, Route] = {}
    for route in sorted(bare, key=_order):
        _claim(routes, _safe(route.tool, route), route)
    for route in sorted(qualify, key=_order):
        _claim(routes, qualified(route.connection, route.tool), route)
    return routes


def _order(route: Route) -> tuple[str, str]:
    return (route.connection, route.tool)


def _hash(route: Route, salt: int = 0) -> str:
    raw = f"{route.connection}\x00{route.tool}\x00{salt}".encode()
    return hashlib.sha256(raw).hexdigest()[:_HASH_CHARS]


def _with_hash(name: str, route: Route, salt: int = 0) -> str:
    return f"{name[: MAX_NAME - _HASH_CHARS - 1]}_{_hash(route, salt)}"


def _safe(name: str, route: Route) -> str:
    """`name` as a provider takes it, capped at MAX_NAME."""
    safe = _UNSAFE.sub("_", name) or "_"
    return safe if len(safe) <= MAX_NAME else _with_hash(safe, route)


def _claim(routes: dict[str, Route], name: str, route: Route) -> None:
    salt = 0
    while name in routes:
        salt += 1
        name = _with_hash(name, route, salt)
    routes[name] = route


def qualified(connection: str, tool: str) -> str:
    """The advertised name for `tool` of `connection`, before any collision
    suffix `build_routes` may add."""
    route = Route(connection, tool)
    return _safe(f"{_UNSAFE.sub('_', connection)}{SEPARATOR}{tool}", route)


def addresses_connection(name: str, connection: str) -> bool:
    """Whether an advertised (or legacy dotted) name is one of `connection`'s."""
    return name.startswith(_UNSAFE.sub("_", connection) + SEPARATOR) or name.startswith(
        connection + _LEGACY_SEPARATOR
    )


def bare_tool(name: str, connection: str) -> str | None:
    """The connection's own tool name inside a qualified `name`, if it is one."""
    for prefix in (_UNSAFE.sub("_", connection) + SEPARATOR, connection + _LEGACY_SEPARATOR):
        if name.startswith(prefix):
            return name[len(prefix) :]
    return None


def resolve(name: str, routes: dict[str, Route]) -> Route | None:
    """The route for a name the model asked for, or None if it has none.

    A model that qualifies a name that did not need qualifying is answered
    anyway -- being stricter than necessary here costs a turn and teaches it
    nothing, and the call is unambiguous either way.
    """
    route = routes.get(name)
    if route is not None:
        return route
    for candidate in routes.values():
        if name in (
            qualified(candidate.connection, candidate.tool),
            f"{candidate.connection}{_LEGACY_SEPARATOR}{candidate.tool}",
        ):
            return candidate
    return None


class _CallableSession(Protocol):
    async def call(self, name: str, arguments: dict[str, Any]) -> str: ...


class RoutedToolset:
    """Several MCP sessions presented as one Toolset.

    Advertised names follow `build_routes`. A call on a qualified (or unique
    bare) name reaches the session that owns the tool, never a neighbour.
    """

    def __init__(
        self,
        sessions: Mapping[str, _CallableSession],
        tools_by_connection: Mapping[str, list[NeutralTool]],
        *,
        auth_by_connection: Mapping[str, Any] | None = None,
        reserved: Collection[str] = frozenset(),
    ) -> None:
        self._sessions = dict(sessions)
        self.auth_by_connection: dict[str, Any] = dict(auth_by_connection or {})
        self._routes = build_routes(
            {name: [t.name for t in tools] for name, tools in tools_by_connection.items()},
            reserved=reserved,
        )
        advertised_of = {(r.connection, r.tool): name for name, r in self._routes.items()}
        advertised: list[NeutralTool] = []
        for conn_name, tools in tools_by_connection.items():
            for tool in tools:
                advertised.append(
                    NeutralTool(
                        name=advertised_of[(conn_name, tool.name)],
                        description=tool.description,
                        parameters=tool.parameters,
                    )
                )
        self.tools = advertised

    def route(self, advertised: str) -> Route | None:
        return resolve(advertised, self._routes)

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        found = self.route(name)
        if found is None:
            raise RuntimeError(f"unknown tool {name!r}")
        return await self._sessions[found.connection].call(found.tool, arguments)
