"""What a run tells the model about its connections.

A connection that fails to list its tools used to vanish into a log line, and
the model then went looking for it through the shell. A skill that names its
tools used to sit next to every other system's full schema. Both are decided
here, before either runtime builds the prompt.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def unavailable_sentence(name: str, reason: str) -> str:
    """One line the model can read. No stack, no second system hidden."""
    line = " ".join(reason.split())
    if len(line) > 200:
        line = line[:200].rstrip()
    return (
        f"Connection {name} is connected but offered no tools: {line}. "
        "Its tools are missing. Do not reach it through the shell."
    )


def notice_for_find(
    unavailable: Sequence[Mapping[str, Any]],
    query: str,
    connection: str | None,
) -> str | None:
    """The dead-connection sentence when a search names that connection."""
    needle = (connection or query).strip().lower()
    if not needle:
        return None
    hits = [
        unavailable_sentence(str(item.get("name", "")), str(item.get("reason", "")))
        for item in unavailable
        if str(item.get("name", "")).strip()
        and str(item.get("name", "")).lower() in needle
    ]
    if not hits:
        return None
    return "\n".join(hits)


def connection_by_tool(routes: Mapping[str, Any] | None) -> dict[str, str]:
    """Advertised tool name -> connection name, from `ctx["tool_routes"]`."""
    if not routes:
        return {}
    out: dict[str, str] = {}
    for advertised, pair in routes.items():
        if isinstance(pair, (list, tuple)) and pair:
            out[str(advertised)] = str(pair[0])
    return out


def allowed_connections_for_skills(
    routes: Mapping[str, Any] | None,
    required_tools: Sequence[str],
) -> set[str] | None:
    """Connections that own a skill's tools, or None when nothing is named.

    None keeps today's list: a run with no required tool still sees every
    connected system. An empty match also keeps the list, so a skill that
    names a tool this run does not have cannot hide the systems it does have.
    """
    required = {name.strip() for name in required_tools if name and name.strip()}
    if not required or not routes:
        return None
    allowed: set[str] = set()
    for advertised, pair in routes.items():
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        connection, real = str(pair[0]), str(pair[1])
        if real in required or str(advertised) in required:
            allowed.add(connection)
    return allowed or None


def pin_skill_tools(pinned: Sequence[str], tool_names: Sequence[str]) -> list[str]:
    """Required tool names join the pin list so the next step offers them."""
    out = list(pinned)
    for name in tool_names:
        if name and name not in out:
            out.append(name)
    return out


def resource_sentence(items: Sequence[Mapping[str, str]]) -> str:
    """Names only. The body stays behind read_resource."""
    lines: list[str] = []
    for item in items:
        connection = item.get("connection", "")
        name = item.get("name") or item.get("uri") or ""
        description = item.get("description") or ""
        if not name:
            continue
        if description:
            lines.append(f"{connection}: {name} — {description}")
        else:
            lines.append(f"{connection}: {name}")
    if not lines:
        return ""
    return (
        "Resources you can open with read_resource "
        "(contents are not loaded yet):\n" + "\n".join(lines)
    )


def prompt_sentence(items: Sequence[Mapping[str, str]]) -> str:
    names = [
        f"{item.get('connection', '')}:{item.get('name', '')}"
        for item in items
        if item.get("name")
    ]
    if not names:
        return ""
    return "Prompt templates on these connections (names only): " + ", ".join(names)
