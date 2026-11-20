"""C6 -- fence successful output returned by external tool connections."""

from __future__ import annotations

from oc8.agent.provenance import fence


def fence_external(output: str, *, source: str) -> str:
    """Mark external material with its connection and tool origin."""
    return fence(output, source=source)
