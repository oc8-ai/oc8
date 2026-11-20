"""MCP list_tools annotations are copied onto NeutralTool (spec §5 B1)."""

from __future__ import annotations

from types import SimpleNamespace

from oc8.agent.mcp_client import _annotations_of
from oc8.modelrouter import NeutralTool


def test_annotations_of_reads_sdk_object_hints() -> None:
    raw = SimpleNamespace(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=None,
    )
    assert _annotations_of(SimpleNamespace(annotations=raw)) == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    }


def test_annotations_of_none_when_missing() -> None:
    assert _annotations_of(SimpleNamespace(annotations=None)) is None
    assert _annotations_of(SimpleNamespace()) is None


def test_neutral_tool_default_annotations_are_none() -> None:
    t = NeutralTool(name="x", description="", parameters={"type": "object"})
    assert t.annotations is None
