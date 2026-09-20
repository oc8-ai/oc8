"""Spec §9.3 snapshot test: for a fixed fixture (agent, connection, caps) the
assembled step-1 messages and the offered tool list are compared to a checked-
in markdown file. Regenerate deliberately with OC8_UPDATE_SNAPSHOTS=1; the
diff is the review artefact for any prompt change in a later package."""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import pytest

from oc8 import models as m
from oc8.agent.control_tools import offered_tools
from oc8.agent.preamble import build_run_preamble
from oc8.modelrouter import NeutralMessage, NeutralTool
from oc8.skills.runtime import load_assigned_skills
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_SNAPSHOT = Path(__file__).parent / "snapshots" / "office_agent_step1.expected.md"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _stable(text: str) -> str:
    text = _UUID.sub("<uuid>", text)
    text = _ISO.sub("<datetime>", text)
    return _DATE.sub("<date>", text)


def _render(messages: list[NeutralMessage], tools: list[NeutralTool]) -> str:
    parts = ["# Step-1 context snapshot", ""]
    for i, msg in enumerate(messages, start=1):
        parts.append(f"## message {i}: role={msg.role}")
        parts.append("")
        content = msg.content if isinstance(msg.content, str) else repr(msg.content)
        parts.append(_stable(content))
        parts.append("")
    parts.append("## offered tools")
    parts.append("")
    for tool in tools:
        parts.append(f"- `{tool.name}` — {_stable(tool.description)}")
    parts.append("")
    return "\n".join(parts)


async def test_step_one_context_matches_the_snapshot(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(
            tenant_id=tenant,
            name="Ops",
            frame={"tools": {"things": {"enabled": True, "read": True, "modify": True}}},
        )
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Nora",
            role_title="Office agent",
            status="running",
            narrowing={},
            definition={},
            presentation={},
        )
        db.add(agent)
        await db.flush()
        conn = m.McpConnection(
            tenant_id=tenant,
            department_id=dept.id,
            name="things",
            transport="stdio",
            server_url="stdio://things",
            connected=True,
            config={"command": "x", "args": []},
            scopes={"read": ["search_records"], "write": ["create_record"]},
        )
        db.add(conn)
        await db.flush()
        assigned = await load_assigned_skills(db, agent=agent, tenant_id=tenant)
        preamble = await build_run_preamble(
            db,
            agent=agent,
            tenant_id=tenant,
            task_text="Look things up and summarise.",
            frame=dept.frame,
            model_locality="eu",
            task_images=[],
            supports_vision=False,
            task=None,
            run_id=None,
        )
        tools = offered_tools(
            agent,
            assigned_skills=assigned,
            active_skills=[],
            mcp_tools=[
                NeutralTool(name="search_records", description="search", parameters={}),
                NeutralTool(name="create_record", description="create", parameters={}),
            ],
            has_knowledge=preamble.has_knowledge,
            has_instruction_files=preamble.has_instruction_files,
            copilot_permissions=preamble.copilot_permissions,
            offer_write_output_file=True,
            offer_run_shell=True,
        )
    rendered = _render(list(preamble.messages), tools)

    if os.environ.get("OC8_UPDATE_SNAPSHOTS") == "1" or not _SNAPSHOT.exists():
        _SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        _SNAPSHOT.write_text(rendered)
    assert rendered == _SNAPSHOT.read_text(), (
        "step-1 context changed; if intended, rerun with OC8_UPDATE_SNAPSHOTS=1 and review the diff"
    )
