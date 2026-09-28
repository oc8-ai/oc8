"""Live check that the five harness cuts show up on a real /step and /tool.

Runs inside the backend container. It does not score the model. It checks the
transcript, the deferred catalog, one resource body, a parked elicitation, and
the same call finished with the operator's answer.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from oc8 import models as m
from oc8.auth.provider import get_identity_provider
from oc8.db.session import tenant_session
from oc8.runtime.registry import BUILTIN_ISOLATED_RUNTIME_REF
from oc8_evals import stack

TENANT = "oc8-community"
MODEL = uuid.UUID("01a04261-dacf-7508-959e-bb024f104ce5")
STATE = Path("/tmp/oc8-harness-probe.json")
BASE = "http://127.0.0.1:8099/api/v1/internal/agent"

SKILL = {
    "oc8_skill": 1,
    "id": "sk-desk",
    "slug": "sk-desk",
    "version": "1.0.0",
    "instruction": "Store the handbook word with store_word on the desk connection.",
    "requires": {"tools": ["store_word"]},
    "code_mode": True,
}


def _post(url: str, token: str, payload: dict | None) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            body = response.read().decode()
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        raise RuntimeError(f"HTTP {exc.code} {url}: {raw}") from exc


def _check(name: str, ok: bool, detail: str) -> bool:
    print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}", flush=True)
    return ok


async def _seed() -> tuple[uuid.UUID, uuid.UUID, str]:
    if STATE.exists():
        STATE.unlink()
    tenant_id = await stack.find_tenant(TENANT)
    env = {
        "PYTHONPATH": "/app/evals",
        "OC8_EVAL_STATE_FILE": str(STATE),
    }
    async with tenant_session(tenant_id) as db:
        dept = m.Department(
            tenant_id=tenant_id,
            name=f"EVAL harness probe {uuid.uuid4().hex[:6]}",
            frame={
                "tools": {
                    "desk": {"enabled": True, "read": True, "modify": True},
                    "archive": {"enabled": True, "read": True, "modify": True},
                    "dead": {"enabled": True, "read": True, "modify": True},
                }
            },
        )
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant_id,
            department_id=dept.id,
            name="Harness probe",
            role_title="Office agent",
            status="running",
            narrowing={},
            definition={
                "max_steps": 8,
                "autonomy": "autonomous",
                "clarify_before_irreversible": False,
                "b5_grants": ["outward"],
            },
            presentation={},
            runtime_ref=BUILTIN_ISOLATED_RUNTIME_REF,
            model_config_id=MODEL,
        )
        db.add(agent)
        await db.flush()

        def _conn(name: str, role: str | None, *, dead: bool = False) -> None:
            if dead:
                config = {"command": "python", "args": ["-c", "import sys; raise SystemExit('connection refused')"]}
            else:
                config = {
                    "command": "python",
                    "args": ["-m", "oc8_evals.mocks.harness_live"],
                    "env": {**env, "OC8_HARNESS_ROLE": role or "desk"},
                }
            db.add(
                m.McpConnection(
                    tenant_id=tenant_id,
                    department_id=dept.id,
                    name=name,
                    transport="stdio",
                    server_url="",
                    scopes={},
                    config=config,
                    connected=True,
                    health={},
                )
            )

        _conn("desk", "desk")
        _conn("archive", "archive")
        _conn("dead", None, dead=True)
        await db.flush()
        version_id = await stack.assign_skill(
            db, tenant_id=tenant_id, agent_id=agent.id, definition=SKILL
        )
        task = m.Task(
            tenant_id=tenant_id,
            department_id=dept.id,
            assigned_agent_id=agent.id,
            title="Harness probe",
            payload={},
            state="in_progress",
        )
        db.add(task)
        await db.flush()
        run = m.AgentRun(
            tenant_id=tenant_id,
            agent_id=agent.id,
            task_id=task.id,
            state="running",
            context={
                "task": "Read the desk handbook with read_resource and store its word.",
                "active_skill_ids": [str(version_id)],
            },
            source="manual",
        )
        db.add(run)
        await db.flush()
        token = get_identity_provider().mint(
            tenant_id=tenant_id,
            subject=f"agent:{agent.id}",
            role="agent_default",
            kind="agent",
            scopes=[f"run:{run.id}"],
        )
        return tenant_id, run.id, token


async def _context(tenant_id: uuid.UUID, run_id: uuid.UUID) -> dict:
    async with tenant_session(tenant_id) as db:
        run = await db.get(m.AgentRun, run_id)
        if run is None:
            raise RuntimeError("run vanished")
        return dict(run.context or {})


async def main() -> int:
    ok = True
    tenant_id, run_id, token = await _seed()
    print(f"run {run_id}", flush=True)
    status, step = _post(f"{BASE}/{run_id}/step", token, None)
    ctx = await _context(tenant_id, run_id)
    transcript = json.dumps(ctx.get("transcript", []))
    catalog = (ctx.get("harness") or {}).get("tool_catalog") or []
    archive = next((card for card in catalog if card.get("name") == "archive_note"), None)
    desk_held = [card for card in catalog if card.get("name") == "store_word"]

    ok &= _check("step", status == 200, f"http {status}")
    ok &= _check(
        "dead-connection",
        "Connection dead is connected but offered no tools" in transcript
        and "Do not reach it through the shell." in transcript,
        "sentence in the first-step transcript",
    )
    ok &= _check(
        "resources",
        "handbook" in transcript and "read_resource" in transcript,
        "resource name listed, body not in the prompt",
    )
    ok &= _check(
        "prompts",
        "desk-brief" in transcript,
        "prompt name listed",
    )
    routes = ctx.get("tool_routes") or {}
    discovery = next(
        (
            msg.get("content", "")
            for msg in ctx.get("transcript") or []
            if isinstance(msg.get("content"), str) and "offered no tools" in msg["content"]
        ),
        "",
    )
    ok &= _check(
        "skill-scope",
        routes.get("archive_note") == ["archive", "archive_note"]
        and "archive_note" not in discovery
        and "contents are not loaded yet" in discovery
        and archive is not None
        and archive.get("connection") == "archive"
        and not desk_held,
        f"route={routes.get('archive_note')} catalog={archive}",
    )
    ok &= _check(
        "code-mode",
        bool(step.get("sdk_py")),
        f"sdk_py length {len(step.get('sdk_py') or '')}",
    )

    _, found = _post(
        f"{BASE}/{run_id}/tools",
        token,
        {"calls": [{"id": "find-1", "name": "find_tools", "arguments": {"query": "dead connection"}}]},
    )
    found_text = (found.get("results") or [{}])[0].get("output", "")
    ok &= _check(
        "find-tools",
        "Connection dead is connected but offered no tools" in found_text,
        found_text.split("\n", 1)[0][:160],
    )

    _, body = _post(
        f"{BASE}/{run_id}/tools",
        token,
        {
            "calls": [
                {
                    "id": "res-1",
                    "name": "read_resource",
                    "arguments": {"connection": "desk", "uri": "office://handbook"},
                }
            ]
        },
    )
    resource_text = (body.get("results") or [{}])[0].get("output", "")
    ok &= _check(
        "read-resource",
        "The handbook says the word is READY." in resource_text
        and "archive_note" not in resource_text,
        resource_text[:180],
    )

    _, parked = _post(
        f"{BASE}/{run_id}/tools",
        token,
        {"calls": [{"id": "ask-1", "name": "need_word", "arguments": {}}]},
    )
    parked_row = (parked.get("results") or [{}])[0]
    ctx = await _context(tenant_id, run_id)
    pending = ctx.get("pending_elicitation") or {}
    ok &= _check(
        "elicitation-park",
        parked_row.get("status") == "waiting_for_input"
        and pending.get("tool") == "need_word"
        and "Which word" in str(pending.get("question", "")),
        f"status={parked_row.get('status')} tool={pending.get('tool')}",
    )

    async with tenant_session(tenant_id) as db:
        run = await db.get(m.AgentRun, run_id)
        if run is None:
            raise RuntimeError("run vanished")
        updated = dict(run.context or {})
        updated["clarifications"] = [{"question": pending.get("question", ""), "answer": "READY"}]
        run.context = updated

    _post(f"{BASE}/{run_id}/step", token, None)
    stored = {}
    if STATE.exists():
        stored = json.loads(STATE.read_text() or "{}")
    ok &= _check(
        "elicitation-replay",
        stored.get("word") == "READY",
        f"state={stored}",
    )

    async with tenant_session(tenant_id) as db:
        run = await db.get(m.AgentRun, run_id)
        if run is not None:
            run.state = "done"
        agent = await db.get(m.Agent, run.agent_id) if run is not None else None
        if agent is not None:
            agent.status = "stopped"
    print("PROBE", "PASS" if ok else "FAIL", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
