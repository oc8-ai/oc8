"""claude_code_runtime against the real Docker driver, with a stub image.

The CLI image is not built in tests, so `claude` is replaced by a shell
script on debian:bookworm-slim that does what matters here: it reads the MCP
config at its first instruction (seed before start), appends to a transcript
in $CLAUDE_CONFIG_DIR, writes /workspace/notes.txt and an output file, and
prints a `result` event. On leg 2 it fails unless notes.txt came back and
output/ did not.
Two legs prove the transcript leg 1 wrote is back for leg 2, on a fresh
sandbox with no host directory behind it, and that it all works as the
image's uid 1000."""

from __future__ import annotations

import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.config import get_settings
from oc8.sandbox.docker_driver import DockerSandboxDriver
from oc8.sandbox.types import SandboxSpec, ScratchDir

PLUGIN_ROOT = Path(__file__).resolve().parents[4] / "capas" / "claude_code_runtime"

_STUB_IMAGE = "debian:bookworm-slim"

#: Replaces `claude`. Fails (exit 3/4) unless the seeded config is already
#: there, carries the token and belongs to uid 1000; counts the transcript's
#: lines so leg 2 can prove leg 1's were restored.
_STUB = r"""
set -e
cfg=/home/node/.config/oc8/mcp.json
[ -f "$cfg" ] || { echo "no config at start" >&2; exit 3; }
grep -q '"Bearer ' "$cfg" || { echo "no token in config" >&2; exit 4; }
[ "$(stat -c %u "$cfg")" = 1000 ] || { echo "config not owned by 1000" >&2; exit 5; }
[ "$(id -u)" = 1000 ]
[ "$PWD" = /workspace ]
if [ -n "$RESUME" ]; then
  grep -q "from leg 1" /workspace/notes.txt || { echo "workspace not restored" >&2; exit 6; }
  [ ! -e /workspace/output/report.txt ] || { echo "output/ was restored" >&2; exit 7; }
else
  echo "from leg 1" > /workspace/notes.txt
fi
t="$CLAUDE_CONFIG_DIR/projects/-workspace/stub-sess.jsonl"
mkdir -p "$(dirname "$t")"
echo "{\"resume\": \"$RESUME\"}" >> "$t"
n=$(wc -l < "$t")
mkdir -p /workspace/output
echo "leg $n" > /workspace/output/report.txt
echo '{"type":"system","subtype":"init","session_id":"stub-sess"}'
printf '{"type":"result","subtype":"success","result":"lines=%s","session_id":"stub-sess"}\n' "$n"
"""


def _evict() -> None:
    for stale in [n for n in sys.modules if n == "runtime" or n.startswith("runtime.")]:
        del sys.modules[stale]


@pytest.fixture(autouse=True)
def _plugin_path() -> Iterator[None]:
    """See test_runtime.py's fixture of the same name."""
    _evict()
    sys.path.insert(0, str(PLUGIN_ROOT))
    yield
    sys.path.remove(str(PLUGIN_ROOT))
    _evict()


class _InitDriver(DockerSandboxDriver):
    def _create(self, spec: Any, run_kwargs: dict[str, object]) -> Any:
        return super()._create(spec, {**run_kwargs, "init": True})

    def _start(self, spec: Any, run_kwargs: dict[str, object]) -> Any:
        return super()._start(spec, {**run_kwargs, "init": True})


async def _agent_and_run(app_session: Any, tenant: uuid.UUID) -> tuple[Any, uuid.UUID]:
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Eng", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Coder",
            status="running",
            narrowing={},
            definition={},
        )
        db.add(agent)
        await db.flush()
        run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state="running", context={})
        db.add(run)
        await db.flush()
        return agent, run.id


async def test_two_legs_on_real_docker_resume_from_the_saved_home(
    _plugin_path: None,
    app_session: Any,
    minio_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import runtime.runtime as rt

    from oc8.runtime.run_context import merge_context

    settings = get_settings()
    monkeypatch.setattr(settings, "sandbox_user", "1000:1000")
    monkeypatch.setattr(settings, "agent_runtime_network", None)
    monkeypatch.setattr(rt, "runtime_image", lambda plugin, fallback: _STUB_IMAGE)
    commands: list[list[str]] = []
    real_build = rt._build_command

    def _stub_command(**kw: Any) -> list[str]:
        commands.append(real_build(**kw))
        resume = kw["resume_session_id"] or ""
        return ["sh", "-c", f"RESUME={resume}; {_STUB}"]

    monkeypatch.setattr(rt, "_build_command", _stub_command)
    # tini as PID 1, as the real CLI images have it (their ENTRYPOINT).
    driver = _InitDriver()
    monkeypatch.setattr(rt, "get_sandbox_driver", lambda: driver)

    tenant = uuid.uuid4()
    agent, run_id = await _agent_and_run(app_session, tenant)

    async with app_session(tenant) as db:
        db.add(agent)
        first = await rt.ClaudeCodeRuntime().execute(
            db, agent=agent, task_text="start", tenant_id=tenant, run_id=run_id
        )
    assert (first.status, first.output) == ("done", "lines=1")

    # As if leg 1 had parked: leg 2 resumes the session it started.
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        await merge_context(db, run, {"cli_harness_sessions": {rt._PLUGIN_NAME: "stub-sess"}})

    async with app_session(tenant) as db:
        db.add(agent)
        second = await rt.ClaudeCodeRuntime().execute(
            db, agent=agent, task_text="carry on", tenant_id=tenant, run_id=run_id
        )
    assert (second.status, second.output) == ("done", "lines=2")
    assert commands[1][commands[1].index("--resume") + 1] == "stub-sess"

    # The output reached attachments without any host directory.
    async with app_session(tenant) as db:
        names = (
            (
                await db.execute(
                    select(m.FileAttachment.filename).where(m.FileAttachment.owner_id == run_id)
                )
            )
            .scalars()
            .all()
        )
    assert "report.txt" in names


async def test_a_stop_after_the_cli_exited_still_saves_the_home_under_tini(
    _plugin_path: None, minio_url: str
) -> None:
    """A parked leg whose CLI had already exited still runs `stop_cli`
    before `save_home`. Under tini that stop used to end the lingering
    sandbox, and the save then read nothing."""
    from cli_harness.seed import load_home, save_home, stop_cli

    driver = _InitDriver()
    handle = await driver.provision(
        SandboxSpec(
            image=_STUB_IMAGE,
            user="1000:1000",
            scratch=(ScratchDir("/home/node/.claude", 16),),
            command=["sh", "-c", "echo '{}' > /home/node/.claude/t.jsonl"],
        )
    )
    try:
        assert await driver.wait(handle, timeout_s=30) == 0
        await stop_cli(driver, handle)
        tenant, run = uuid.uuid4(), uuid.uuid4()
        assert await save_home(
            driver,
            handle,
            tenant_id=tenant,
            run_id=run,
            name="claude-home",
            path="/home/node/.claude",
        )
        assert await load_home(tenant_id=tenant, run_id=run, name="claude-home") == {
            "t.jsonl": b"{}\n"
        }
    finally:
        await driver.teardown(handle)
