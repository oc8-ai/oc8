"""C2 error shaping (spec §6 C2). Never converts a failure into success;
never drops the original message. The two extra trailing lines are kind-
specific; the envelope itself is shared."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ToolErrorKind = Literal["mcp", "timeout", "deny", "control"]

_MCP_HINT = (
    "Read the error, fix the arguments or choose another tool; do not repeat the identical call."
)
_TIMEOUT_HINT = "the call may have partially executed — read the record before retrying"


@dataclass(frozen=True)
class ToolError:
    kind: ToolErrorKind
    message: str
    duration_s: float | None = None


def render_error(tool: str, source: str, err: ToolError) -> str:
    lines = [f"ERROR from {source} ({tool}): {err.message}"]
    if err.kind == "mcp":
        lines.append(_MCP_HINT)
    elif err.kind == "timeout":
        duration = f" after {err.duration_s:g}s" if err.duration_s is not None else ""
        lines.append(f"timed out{duration} — {_TIMEOUT_HINT}")
    return "\n".join(lines)


def classify_exception(exc: BaseException, *, duration_s: float | None = None) -> ToolError:
    import httpx

    from oc8.agent.mcp_client import McpToolError

    if isinstance(exc, McpToolError):
        return ToolError(kind="mcp", message=str(exc))
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return ToolError(
            kind="timeout",
            message=str(exc) or "timed out",
            duration_s=duration_s,
        )
    return ToolError(kind="mcp", message=str(exc))
