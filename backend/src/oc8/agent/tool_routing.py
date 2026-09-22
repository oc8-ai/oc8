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
  `<connection>.<tool>`, and the bare form is not offered at all. The model
  cannot express the ambiguous call, so it cannot make it.

The core still names no software. It only knows that connections have names and
that tools have names.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from oc8.modelrouter.types import NeutralTool

#: Separates a connection from a tool in an advertised name. A dot is safe: MCP
#: tool names are `[a-zA-Z0-9_-]` by convention, so a dot cannot collide with a
#: name a server already uses.
SEPARATOR = "."


@dataclass(frozen=True)
class Route:
    """Where an advertised tool name actually goes."""

    connection: str
    tool: str


def build_routes(offered: dict[str, list[str]]) -> dict[str, Route]:
    """Advertised name -> route, given each connection's own tool names.

    `offered` maps connection name to the tools that connection exposes. Order
    is irrelevant: the result depends only on which names collide, so two runs
    over the same connections always advertise the same names.
    """
    owners: dict[str, list[str]] = defaultdict(list)
    for connection, tools in offered.items():
        for tool in tools:
            owners[tool].append(connection)

    routes: dict[str, Route] = {}
    for tool, connections in owners.items():
        if len(connections) == 1:
            routes[tool] = Route(connections[0], tool)
            continue
        for connection in connections:
            routes[qualified(connection, tool)] = Route(connection, tool)
    return routes


def qualified(connection: str, tool: str) -> str:
    return f"{connection}{SEPARATOR}{tool}"


def resolve(name: str, routes: dict[str, Route]) -> Route | None:
    """The route for a name the model asked for, or None if it has none.

    A model that qualifies a name that did not need qualifying is answered
    anyway -- being stricter than necessary here costs a turn and teaches it
    nothing, and the call is unambiguous either way.
    """
    route = routes.get(name)
    if route is not None:
        return route
    if SEPARATOR in name:
        connection, _, tool = name.partition(SEPARATOR)
        candidate = routes.get(tool)
        if candidate is not None and candidate.connection == connection:
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
    ) -> None:
        self._sessions = dict(sessions)
        self.auth_by_connection: dict[str, Any] = dict(auth_by_connection or {})
        self._routes = build_routes(
            {name: [t.name for t in tools] for name, tools in tools_by_connection.items()}
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
