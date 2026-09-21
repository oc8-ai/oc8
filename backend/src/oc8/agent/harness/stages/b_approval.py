"""B5 -- approval posture and human-readable previews."""

from __future__ import annotations

from typing import Any, Literal

ApprovalPosture = Literal["allow", "ask"]


def strip_justification(arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
    args = dict(arguments)
    justification = str(args.pop("justification", "") or "")
    return args, justification


def autonomy_of(definition: dict[str, Any] | None) -> str:
    raw = (definition or {}).get("autonomy", "default")
    return raw if raw in {"conservative", "default", "autonomous"} else "default"


def posture(tier: str, autonomy: str, *, granted: bool) -> ApprovalPosture:
    if tier == "read":
        return "allow"
    if autonomy == "conservative":
        return "ask"
    if tier == "write":
        return "allow"
    if autonomy != "autonomous":
        return "ask"
    if tier == "outward" and not granted:
        return "ask"
    return "allow"


class _Safe(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return ""


def render_preview(
    *,
    tool: str,
    connection: str,
    arguments: dict[str, Any],
    template: str | None,
    record: str,
) -> str:
    text = template or "Allow {connection} to run {tool} on {record}?"
    values = _Safe({key: str(value) for key, value in arguments.items()})
    values.update(connection=connection, tool=tool, record=record)
    return text.format_map(values)
