"""Files across the MCP boundary: binary content a tool returns becomes a file
of the current run, and an argument the tool marks `x-oc8-file` takes a run
file's id, which core swaps for the file's content before the call leaves.

A real stdio MCP server, so the SDK's own content models are what is parsed,
and a real object store (MinIO), so the bytes really go there and back."""

from __future__ import annotations

import base64
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.mcp_client import McpSession, McpToolError
from oc8.agent.run_files import FILE_MARKER, FileReferenceError, RunFiles, bind_run_files
from oc8.storage import s3
from oc8.storage.attachments import store_attachment_bytes

pytestmark = pytest.mark.asyncio

PDF = b"%PDF-1.4 binary \x00\x01\x02 body" * 40
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(64))

_SERVER = """
import asyncio
import base64
import json

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

PDF = {pdf!r}
PNG = {png!r}


def b64(data):
    return base64.b64encode(data).decode()


TOOLS = [
    types.Tool(name="fetch", description="", inputSchema={{"type": "object", "properties": {{}}}}),
    types.Tool(
        name="send",
        description="Echoes its arguments.",
        inputSchema={{
            "type": "object",
            "properties": {{
                "to": {{"type": "string"}},
                "attachments": {{
                    "type": "array",
                    "items": {{
                        "type": "object",
                        "properties": {{
                            "file": {{
                                "type": "string",
                                "description": "A file.",
                                "x-oc8-file": True,
                            }},
                        }},
                    }},
                }},
                "cover": {{"type": "string", "x-oc8-file": True}},
            }},
        }},
    ),
]


async def on_list(ctx, params):
    return types.ListToolsResult(tools=TOOLS)


async def on_call(ctx, params):
    if params.name == "fetch":
        return types.CallToolResult(
            content=[
                types.TextContent(type="text", text='{{"mail": "m1"}}'),
                types.EmbeddedResource(
                    type="resource",
                    resource=types.BlobResourceContents(
                        uri="attachment:///invoice%20march.pdf",
                        mime_type="application/pdf",
                        blob=b64(PDF),
                    ),
                ),
                types.ImageContent(type="image", data=b64(PNG), mime_type="image/png"),
                types.EmbeddedResource(
                    type="resource",
                    resource=types.BlobResourceContents(
                        uri="attachment:///tool.exe",
                        mime_type="application/x-msdownload",
                        blob=b64(b"MZ" * 10),
                    ),
                ),
            ]
        )
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(params.arguments))]
    )


server = Server("files", on_list_tools=on_list, on_call_tool=on_call)


async def run():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


asyncio.run(run())
"""


def _server(tmp_path: Path) -> tuple[str, list[str]]:
    script = tmp_path / "server.py"
    script.write_text(_SERVER.format(pdf=PDF, png=PNG))
    return sys.executable, [str(script)]


async def _agent_and_run(db: Any, tenant: uuid.UUID) -> tuple[m.Agent, m.AgentRun]:
    dept = m.Department(tenant_id=tenant, name="Support", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name="Nora",
        status="running",
        definition={},
        presentation={},
        narrowing={},
    )
    db.add(agent)
    await db.flush()
    run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state="running")
    db.add(run)
    await db.flush()
    return agent, run


async def _stored(
    app_session: Any, tenant: uuid.UUID, owner_type: str, owner_id: uuid.UUID, name: str
) -> m.FileAttachment:
    async with app_session(tenant) as db:
        return await store_attachment_bytes(
            db,
            tenant_id=tenant,
            owner_type=owner_type,
            owner_id=owner_id,
            filename=name,
            raw=PDF,
            content_type="application/pdf",
        )


async def _setup(app_session: Any) -> tuple[uuid.UUID, m.Agent, m.AgentRun]:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run = await _agent_and_run(db, tenant)
    return tenant, agent, run


def _files(app_session: Any, tenant: uuid.UUID, agent: m.Agent, run: m.AgentRun) -> RunFiles:
    return RunFiles(tenant_id=tenant, run_id=run.id, agent_id=agent.id, open_session=app_session)


# --- incoming -----------------------------------------------------------------


async def test_binary_content_becomes_a_file_of_the_run(
    tmp_path: Path, app_session: Any, minio_url: str
) -> None:
    tenant, agent, run = await _setup(app_session)
    command, args = _server(tmp_path)
    async with McpSession(command, args) as session:
        with bind_run_files(_files(app_session, tenant, agent, run)):
            output = await session.call("fetch", {})

    async with app_session(tenant) as db:
        rows = (
            (
                await db.execute(
                    select(m.FileAttachment).where(
                        m.FileAttachment.owner_type == "agent_run",
                        m.FileAttachment.owner_id == run.id,
                    )
                )
            )
            .scalars()
            .all()
        )
    by_name = {row.filename: row for row in rows}
    assert set(by_name) == {"invoice march.pdf", "image-2.png"}
    pdf = by_name["invoice march.pdf"]
    assert pdf.content_type == "application/pdf" and pdf.size_bytes == len(PDF)
    assert await s3.get_object(pdf.bucket_key) == PDF
    assert await s3.get_object(by_name["image-2.png"].bucket_key) == PNG

    # The model reads a reference, never the bytes.
    assert '{"mail": "m1"}' in output
    assert str(pdf.id) in output and "invoice march.pdf" in output
    assert str(by_name["image-2.png"].id) in output
    assert base64.b64encode(PDF).decode()[:40] not in output
    # A type run files do not take is named, not stored and not dropped silently.
    assert "tool.exe" in output and "not kept" in output


async def test_binary_content_outside_a_run_is_named_not_kept(tmp_path: Path) -> None:
    command, args = _server(tmp_path)
    async with McpSession(command, args) as session:
        output = await session.call("fetch", {})
    assert "invoice march.pdf" in output and "not kept" in output
    assert base64.b64encode(PDF).decode()[:40] not in output


# --- outgoing -----------------------------------------------------------------


async def test_the_marker_is_not_shown_to_the_model(tmp_path: Path) -> None:
    command, args = _server(tmp_path)
    async with McpSession(command, args) as session:
        [send] = [t for t in session.tools if t.name == "send"]
    assert FILE_MARKER not in json.dumps(send.parameters)
    cover = send.parameters["properties"]["cover"]
    assert cover["type"] == "string" and "file id" in cover["description"]
    item = send.parameters["properties"]["attachments"]["items"]["properties"]["file"]
    assert item["description"].startswith("A file.") and "file id" in item["description"]


async def test_a_file_reference_reaches_the_tool_as_the_files_content(
    tmp_path: Path, app_session: Any, minio_url: str
) -> None:
    tenant, agent, run = await _setup(app_session)
    own = await _stored(app_session, tenant, "agent_run", run.id, "report.pdf")
    instructions = await _stored(app_session, tenant, "agent_instructions", agent.id, "policy.pdf")
    command, args = _server(tmp_path)
    async with McpSession(command, args) as session:
        with bind_run_files(_files(app_session, tenant, agent, run)):
            output = await session.call(
                "send",
                {
                    "to": "alice@example.com",
                    "attachments": [{"file": str(own.id)}, {"file": "policy.pdf"}],
                    "cover": f"file:{own.id}",
                },
            )
    sent = json.loads(output)
    assert sent["to"] == "alice@example.com"
    first, second = sent["attachments"]
    assert first["file"] == {
        "file_id": str(own.id),
        "name": "report.pdf",
        "mime_type": "application/pdf",
        "size": len(PDF),
        "content_base64": base64.b64encode(PDF).decode(),
    }
    assert second["file"]["file_id"] == str(instructions.id)
    assert sent["cover"]["name"] == "report.pdf"


# --- which files a run may use (Ruling 15) ---------------------------------------
#
# Another run's files are as private as that run: a member's run sees her own
# other runs' files, an autonomous run the agent's other autonomous runs' --
# never a colleague's, and never across that line in either direction.

ANNA = {"originating_operator": "anna"}
BEN = {"originating_operator": "ben"}


async def _run(
    app_session: Any,
    tenant: uuid.UUID,
    agent: m.Agent,
    *,
    source: str = "manual",
    context: dict[str, Any] | None = None,
) -> m.AgentRun:
    async with app_session(tenant) as db:
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            state="running",
            source=source,
            context=dict(context or {}),
        )
        db.add(run)
        await db.flush()
    return run


async def _members_chat(
    app_session: Any, tenant: uuid.UUID, agent: m.Agent, subject: str
) -> m.ChatSession:
    async with app_session(tenant) as db:
        member = m.OrgMember(tenant_id=tenant, subject=subject, subject_uuid=uuid.uuid4())
        db.add(member)
        await db.flush()
        chat = m.ChatSession(tenant_id=tenant, agent_id=agent.id, member_id=member.id)
        db.add(chat)
        await db.flush()
    return chat


async def _resolves(files: RunFiles, ref: str) -> str | None:
    """The file id `ref` resolves to for `files`' run, None when refused."""
    try:
        [found] = await files.resolve_many([ref])
    except FileReferenceError:
        return None
    return str(found["file_id"])


@pytest.mark.parametrize(
    ("earlier", "current"),
    [
        (("manual", ANNA), ("manual", ANNA)),
        (("cron", {}), ("event", {})),
    ],
    ids=["same_member", "autonomous"],
)
async def test_a_file_of_a_run_alike_is_visible_by_id(
    earlier: tuple[str, dict[str, Any]],
    current: tuple[str, dict[str, Any]],
    app_session: Any,
    minio_url: str,
) -> None:
    tenant, agent, _ = await _setup(app_session)
    before = await _run(app_session, tenant, agent, source=earlier[0], context=earlier[1])
    run = await _run(app_session, tenant, agent, source=current[0], context=current[1])
    produced = await _stored(app_session, tenant, "agent_run", before.id, "earlier.pdf")
    files = _files(app_session, tenant, agent, run)
    assert await _resolves(files, str(produced.id)) == str(produced.id)


@pytest.mark.parametrize(
    ("earlier", "current"),
    [
        (("manual", ANNA), ("manual", BEN)),
        (("manual", ANNA), ("cron", {})),
        (("cron", {}), ("manual", ANNA)),
        (("manual", {}), ("manual", {})),
    ],
    ids=["colleague", "member_to_autonomous", "autonomous_to_member", "nobodys"],
)
async def test_a_file_of_another_kind_of_run_is_refused_by_id_and_name(
    earlier: tuple[str, dict[str, Any]],
    current: tuple[str, dict[str, Any]],
    app_session: Any,
    minio_url: str,
) -> None:
    tenant, agent, _ = await _setup(app_session)
    before = await _run(app_session, tenant, agent, source=earlier[0], context=earlier[1])
    run = await _run(app_session, tenant, agent, source=current[0], context=current[1])
    theirs = await _stored(app_session, tenant, "agent_run", before.id, "theirs.pdf")
    files = _files(app_session, tenant, agent, run)
    assert await _resolves(files, str(theirs.id)) is None
    assert await _resolves(files, f"file:{theirs.id}") is None
    assert await _resolves(files, "theirs.pdf") is None


async def test_an_autonomous_run_cannot_use_a_members_chat_run_file(
    app_session: Any, minio_url: str
) -> None:
    tenant, agent, _ = await _setup(app_session)
    chat = await _members_chat(app_session, tenant, agent, "anna")
    chat_run = await _run(
        app_session,
        tenant,
        agent,
        source="chat",
        context={**ANNA, "chat_session_id": str(chat.id)},
    )
    theirs = await _stored(app_session, tenant, "agent_run", chat_run.id, "offer.pdf")
    run = await _run(app_session, tenant, agent, source="cron")
    files = _files(app_session, tenant, agent, run)
    assert await _resolves(files, str(theirs.id)) is None
    # ... while her own next chat turn may.
    mine = await _run(
        app_session,
        tenant,
        agent,
        source="chat",
        context={**ANNA, "chat_session_id": str(chat.id)},
    )
    assert await _resolves(_files(app_session, tenant, agent, mine), str(theirs.id)) == str(
        theirs.id
    )


@pytest.mark.parametrize("whose", ["other_tenant", "other_agent", "unknown"])
async def test_a_file_the_run_may_not_see_is_refused_before_the_call(
    whose: str, tmp_path: Path, app_session: Any, minio_url: str
) -> None:
    tenant, agent, run = await _setup(app_session)
    if whose == "other_tenant":
        other_tenant, _, other_run = await _setup(app_session)
        foreign = await _stored(app_session, other_tenant, "agent_run", other_run.id, "x.pdf")
        ref = str(foreign.id)
    elif whose == "other_agent":
        async with app_session(tenant) as db:
            _, other_run = await _agent_and_run(db, tenant)
        foreign = await _stored(app_session, tenant, "agent_run", other_run.id, "x.pdf")
        ref = str(foreign.id)
    else:
        ref = str(uuid.uuid4())
    command, args = _server(tmp_path)
    async with McpSession(command, args) as session:
        with bind_run_files(_files(app_session, tenant, agent, run)):
            with pytest.raises(McpToolError, match="no file") as caught:
                await session.call("send", {"cover": ref})
    assert "content_base64" not in str(caught.value)


async def test_a_file_reference_outside_a_run_is_refused(tmp_path: Path) -> None:
    command, args = _server(tmp_path)
    async with McpSession(command, args) as session:
        with pytest.raises(McpToolError, match="run"):
            await session.call("send", {"cover": str(uuid.uuid4())})


async def test_arguments_without_files_pass_unchanged(tmp_path: Path) -> None:
    command, args = _server(tmp_path)
    async with McpSession(command, args) as session:
        output = await session.call("send", {"to": "a@example.com", "attachments": []})
    assert json.loads(output) == {"to": "a@example.com", "attachments": []}


# --- per-call caps --------------------------------------------------------------


async def test_a_repeated_file_is_loaded_once_and_its_bytes_count_once(
    tmp_path: Path, app_session: Any, minio_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.agent import run_files

    tenant, agent, run = await _setup(app_session)
    own = await _stored(app_session, tenant, "agent_run", run.id, "report.pdf")
    # Room for the file once, not twice: a repeat must not count again.
    monkeypatch.setattr(run_files, "MAX_FILE_BYTES_PER_CALL", len(PDF) + 1)
    loads: list[str] = []
    real_get = s3.get_object

    async def counting_get(key: str) -> bytes:
        loads.append(key)
        return await real_get(key)

    monkeypatch.setattr(s3, "get_object", counting_get)
    command, args = _server(tmp_path)
    async with McpSession(command, args) as session:
        with bind_run_files(_files(app_session, tenant, agent, run)):
            output = await session.call(
                "send",
                {
                    "attachments": [{"file": str(own.id)}, {"file": "report.pdf"}],
                    "cover": str(own.id),
                },
            )
    sent = json.loads(output)
    assert [a["file"]["file_id"] for a in sent["attachments"]] == [str(own.id)] * 2
    assert sent["cover"]["content_base64"] == base64.b64encode(PDF).decode()
    assert loads == [own.bucket_key]


async def test_too_many_references_are_refused_before_anything_loads(
    tmp_path: Path, app_session: Any, minio_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.agent import run_files

    tenant, agent, run = await _setup(app_session)
    own = await _stored(app_session, tenant, "agent_run", run.id, "report.pdf")
    loads: list[str] = []

    async def counting_get(key: str) -> bytes:
        loads.append(key)
        return PDF

    monkeypatch.setattr(s3, "get_object", counting_get)
    too_many = run_files.MAX_FILE_REFERENCES + 1
    command, args = _server(tmp_path)
    async with McpSession(command, args) as session:
        with bind_run_files(_files(app_session, tenant, agent, run)):
            with pytest.raises(McpToolError, match=f"at most {run_files.MAX_FILE_REFERENCES}"):
                await session.call("send", {"attachments": [{"file": str(own.id)}] * too_many})
    assert loads == []


async def test_too_many_bytes_are_refused_before_anything_loads(
    tmp_path: Path, app_session: Any, minio_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.agent import run_files

    tenant, agent, run = await _setup(app_session)
    first = await _stored(app_session, tenant, "agent_run", run.id, "a.pdf")
    second = await _stored(app_session, tenant, "agent_run", run.id, "b.pdf")
    monkeypatch.setattr(run_files, "MAX_FILE_BYTES_PER_CALL", 2 * len(PDF) - 1)
    loads: list[str] = []

    async def counting_get(key: str) -> bytes:
        loads.append(key)
        return PDF

    monkeypatch.setattr(s3, "get_object", counting_get)
    command, args = _server(tmp_path)
    async with McpSession(command, args) as session:
        with bind_run_files(_files(app_session, tenant, agent, run)):
            with pytest.raises(McpToolError, match="together"):
                await session.call(
                    "send",
                    {"attachments": [{"file": str(first.id)}, {"file": str(second.id)}]},
                )
    assert loads == []


def test_the_caps_are_what_the_contract_says() -> None:
    from oc8.agent import run_files

    assert run_files.MAX_FILE_REFERENCES == 20
    assert run_files.MAX_FILE_BYTES_PER_CALL == 50 * 1024 * 1024


# --- by name: own files and the agent's instructions only ------------------------


async def test_a_name_finds_only_the_runs_own_files_and_the_instructions(
    app_session: Any, minio_url: str
) -> None:
    tenant, agent, _ = await _setup(app_session)
    policy = await _stored(app_session, tenant, "agent_instructions", agent.id, "AGB.pdf")
    # An earlier run of the same member kept a mail attachment of the same
    # name -- later, so newest-first would have picked it.
    earlier = await _run(app_session, tenant, agent, context=ANNA)
    spoof = await _stored(app_session, tenant, "agent_run", earlier.id, "AGB.pdf")
    run = await _run(app_session, tenant, agent, context=ANNA)
    own = await _stored(app_session, tenant, "agent_run", run.id, "report.pdf")
    files = _files(app_session, tenant, agent, run)

    assert await _resolves(files, "AGB.pdf") == str(policy.id)
    assert await _resolves(files, "report.pdf") == str(own.id)
    # Another run's file is reachable by its id only.
    assert await _resolves(files, "AGB.pdf") != str(spoof.id)
    assert await _resolves(files, str(spoof.id)) == str(spoof.id)


async def test_a_name_several_files_carry_is_refused(app_session: Any, minio_url: str) -> None:
    tenant, agent, run = await _setup(app_session)
    await _stored(app_session, tenant, "agent_instructions", agent.id, "AGB.pdf")
    await _stored(app_session, tenant, "agent_run", run.id, "AGB.pdf")
    files = _files(app_session, tenant, agent, run)
    with pytest.raises(FileReferenceError, match=r"Several files are named 'AGB\.pdf'"):
        await files.resolve_many(["AGB.pdf"])


# --- chat attachments -------------------------------------------------------------


async def test_the_runs_own_chat_attachments_are_visible_by_id(
    app_session: Any, minio_url: str
) -> None:
    tenant, agent, _ = await _setup(app_session)
    chat = await _members_chat(app_session, tenant, agent, "anna")
    other_chat = await _members_chat(app_session, tenant, agent, "anna-too")
    async with app_session(tenant) as db:
        message = m.ChatMessage(
            tenant_id=tenant, session_id=chat.id, role="user", content="see attached"
        )
        db.add(message)
        await db.flush()
    pending = await _stored(app_session, tenant, "chat_message", chat.id, "draft.pdf")
    sent = await _stored(app_session, tenant, "chat_message", message.id, "sent.pdf")
    elsewhere = await _stored(app_session, tenant, "chat_message", other_chat.id, "x.pdf")
    run = await _run(
        app_session, tenant, agent, source="chat", context={**ANNA, "chat_session_id": str(chat.id)}
    )
    async with app_session(tenant) as db:
        stored = await db.get(m.AgentRun, run.id)
        files = RunFiles.of(stored)
    files = RunFiles(
        tenant_id=files.tenant_id,
        run_id=files.run_id,
        agent_id=files.agent_id,
        chat_session_id=files.chat_session_id,
        open_session=app_session,
    )
    assert files.chat_session_id == chat.id
    assert await _resolves(files, str(pending.id)) == str(pending.id)
    assert await _resolves(files, str(sent.id)) == str(sent.id)
    assert await _resolves(files, str(elsewhere.id)) is None
    # Not by name: a chat attachment is no file of the run.
    assert await _resolves(files, "sent.pdf") is None
