from oc8.modelrouter.types import NeutralTool
from oc8.agent.harness.sdk import render_oc8_tools


def test_render_is_deterministic_and_posts_tool():
    tool = NeutralTool(
        name="search_records",
        description="Find records.",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
    text = render_oc8_tools([tool])
    assert text == render_oc8_tools([tool])
    assert "def search_records" in text
    assert "Find records." in text
    assert "OC8_INTERNAL_URL" in text
    assert "OC8_AGENT_TOKEN" in text
    assert "/api/v1/internal/agent/" in text
    assert "class ToolDenied" in text
    assert "class ApprovalRequired" in text
    assert "def find_tools" in text


def test_render_skips_invalid_python_names():
    tool = NeutralTool(name="not-a-name", description="x", parameters={"type": "object", "properties": {}})
    assert "def not-a-name" not in render_oc8_tools([tool])
