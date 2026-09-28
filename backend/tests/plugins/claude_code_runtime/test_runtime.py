from __future__ import annotations

import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cli_harness.toolchain import TOOLCHAIN_NOTE

from oc8 import models as m
from oc8.sandbox.types import ExecResult, SandboxHandle

pytestmark = pytest.mark.asyncio

# tests/plugins/claude_code_runtime/test_runtime.py -> repo root -> the plugin folder.
# This plugin is NOT on pytest's `pythonpath` any more (see backend/pyproject.toml):
# every plugin resolves its own package from its own test module, because after the
# package restructure four plugins share the folder name `runtime/` and a blanket path
# list would silently hand `import runtime` to whichever root sorts first.
PLUGIN_ROOT = Path(__file__).resolve().parents[4] / "capas" / "claude_code_runtime"


def _evict() -> None:
    """Drop every cached `runtime`/`runtime.*` module from sys.modules.

    `runtime` is the package name EVERY `runtime_adapter` plugin now ships --
    claude_code_runtime, codex_runtime, opencode_runtime and nanoclaw_runtime,
    four plugins behind one top-level module name (design §2) -- and
    sys.modules is keyed by NAME, not by path. Called SYMMETRICALLY, on
    fixture setup AND teardown: before, so a sibling runtime's cached copy
    cannot answer our import; after, so nothing generic is left cached for
    anyone else. The teardown half is the load-bearing one --
    `loader.import_entry_point` (Task 6's collision fix) only evicts modules IT
    ITSELF introduced, so a `runtime` left cached here makes a later
    `find_plugin`/`load_plugin` for one of the other three silently hand back
    THIS plugin's `register`.
    """
    for _stale in [n for n in sys.modules if n == "runtime" or n.startswith("runtime.")]:
        del sys.modules[_stale]


@pytest.fixture(autouse=True)
def _plugin_path() -> Iterator[None]:
    """Make THIS plugin's package the one that resolves, for the duration of each test.

    `runtime`/`connector`/`channel`/`provider`/`mcp_bridge` are generic names every
    plugin reuses (design §2), and sys.modules is keyed by NAME, not by path -- so a
    sibling plugin's tests would otherwise hand us ITS code. This is a FUNCTION-scoped
    autouse fixture, deliberately, not a module-top prelude: every one of this file's
    ~16 plugin imports sits INSIDE a test body and resolves at execution time, long
    after collection-time module-top code has run, when sys.path[0] is whichever
    plugin's test module was imported last. A module-top prelude silently does nothing
    for this file. The already-imported module objects other test files hold stay valid
    across the eviction.

    This plugin's package IS the generic `runtime/` now (the folder convention for
    every `runtime_adapter` plugin), which is exactly why the eviction below is
    mandatory rather than defensive: `import runtime` is ambiguous across four
    plugin roots and only this fixture makes it mean THIS one.
    """
    _evict()
    sys.path.insert(0, str(PLUGIN_ROOT))
    yield
    sys.path.remove(str(PLUGIN_ROOT))
    _evict()


@pytest.fixture(autouse=True)
def _use_tmp_session_root(
    _plugin_path: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """execute() now creates its bind-mount SOURCES on the host before
    provision() -- /workspace's directory and the per-run $CLAUDE_CONFIG_DIR
    that makes --resume able to find anything at all (see runtime.py's
    _config_home_dir and cli_harness.dirs) -- so it does REAL disk I/O even
    against a FakeDriver. The real runtime_session_root default
    (/var/lib/oc8/sessions) is neither writable nor expected to exist outside
    a deployed container, so every test redirects it to a pytest tmp_path,
    the same way codex_runtime's and opencode_runtime's tests already do (and
    nanoclaw_runtime's before them). Autouse, because there is no longer a
    test in this file that can run without it.

    Takes `_plugin_path` as an ARGUMENT, not for its value but for the ordering:
    `monkeypatch.setattr` with a dotted string resolves through `__import__` and
    therefore through the `sys.modules` cache at fixture-setup time, so if this ran
    first it would patch whichever plugin's module happened to be cached. A declared
    dependency is the only ordering guarantee pytest gives -- declaration order is
    not one."""
    monkeypatch.setattr("runtime.runtime.SESSION_ROOT_OVERRIDE", str(tmp_path))


class _FakeDriver:
    """Plays the claude-code container: 'runs' by returning canned
    stream-json lines from logs(), then reports exit via wait(). Mirrors
    nanoclaw's own FakeDriver pattern (backend/tests/plugins/nanoclaw/
    test_runtime.py) but for a stdout-streaming harness instead of a
    SQLite-backed session."""

    def __init__(self, stdout_lines: list[str], *, exit_code: int = 0) -> None:
        self.stdout_lines = stdout_lines
        self.exit_code = exit_code
        self.specs: list[Any] = []
        self.torn_down = 0

    async def provision(self, spec: Any) -> SandboxHandle:
        self.specs.append(spec)
        return SandboxHandle(container_id="claude-container", image=spec.image)

    async def logs(self, handle: SandboxHandle) -> str:
        return "".join(line + "\n" for line in self.stdout_lines)

    async def wait(self, handle: SandboxHandle, timeout_s: float = 600.0) -> int:
        return self.exit_code

    async def exec(self, handle: SandboxHandle, command: list[str], **kw: Any) -> ExecResult:
        raise NotImplementedError

    async def fs_write(self, handle: SandboxHandle, path: str, content: bytes) -> None:
        raise NotImplementedError

    async def fs_read(self, handle: SandboxHandle, path: str) -> bytes:
        raise NotImplementedError

    async def teardown(self, handle: SandboxHandle) -> None:
        self.torn_down += 1

    async def reap_orphans(self) -> int:
        return 0


class _NeverFinishingDriver(_FakeDriver):
    """wait() always raises -- exactly how docker_driver.py's own wait()
    signals "still running" under a short timeout_s (the poll loop passes
    POLL_INTERVAL_S, not the run's real deadline). Simulates a wedged
    `claude` CLI process that never exits, to prove the run-level timeout
    -- not driver.wait()'s own short poll timeout -- is what eventually
    ends the run."""

    async def wait(self, handle: SandboxHandle, timeout_s: float = 600.0) -> int:
        raise TimeoutError("still running")


def _install(monkeypatch: pytest.MonkeyPatch, driver: Any) -> None:
    monkeypatch.setattr("runtime.runtime.get_sandbox_driver", lambda: driver)


async def test_a_finished_run_returns_done_and_tears_down(
    app_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime.runtime import ClaudeCodeRuntime

    result_event = (
        '{"type":"result","subtype":"success","result":"Fixed the bug.",'
        '"session_id":"claude-sess-1"}'
    )
    driver = _FakeDriver([result_event], exit_code=0)
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
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
        run_id = run.id

        result = await ClaudeCodeRuntime().execute(
            db,
            agent=agent,
            task_text="fix the bug",
            tenant_id=tenant,
            run_id=run_id,
        )

    assert result.status == "done"
    assert result.output == "Fixed the bug."
    assert driver.torn_down == 1
    spec = driver.specs[0]
    assert spec.network_disabled is False
    assert "--output-format" in spec.command and "stream-json" in spec.command
    assert "--resume" not in spec.command  # first leg, nothing to resume


async def test_a_parked_run_persists_the_session_id_and_tears_down(
    app_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The harness calls ask_user via MCP (Task 1); the gateway parks the run
    (writes isolated_result) independently of anything this test simulates
    directly on the DB row -- exercising exactly what the plugin's poll loop
    must react to."""
    from runtime.runtime import ClaudeCodeRuntime

    driver = _FakeDriver(['{"type":"system","subtype":"init","session_id":"claude-sess-2"}'])
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
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
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            state="running",
            context={"isolated_result": {"status": "waiting_for_input", "output": "which file?"}},
        )
        db.add(run)
        await db.flush()
        run_id = run.id

        result = await ClaudeCodeRuntime().execute(
            db,
            agent=agent,
            task_text="fix the bug",
            tenant_id=tenant,
            run_id=run_id,
        )

    assert result.status == "waiting_for_input"
    assert driver.torn_down == 1
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        from cli_harness.session_state import get_session_id

        assert get_session_id(run, "claude_code_runtime") == "claude-sess-2"


async def test_a_resume_leg_passes_the_resume_flag_and_the_session_id(
    app_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime.runtime import ClaudeCodeRuntime

    driver = _FakeDriver(
        ['{"type":"result","subtype":"success","result":"Done.","session_id":"claude-sess-2"}']
    )
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
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
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            state="running",
            context={"cli_harness_sessions": {"claude_code_runtime": "claude-sess-2"}},
        )
        db.add(run)
        await db.flush()
        run_id = run.id

        await ClaudeCodeRuntime().execute(
            db,
            agent=agent,
            task_text="fix the bug",
            tenant_id=tenant,
            run_id=run_id,
        )

    spec = driver.specs[0]
    assert "--resume" in spec.command
    resume_index = spec.command.index("--resume")
    assert spec.command[resume_index + 1] == "claude-sess-2"


async def test_a_nonzero_exit_is_a_failed_result(
    app_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime.runtime import ClaudeCodeRuntime

    driver = _FakeDriver(["not valid json"], exit_code=1)
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
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
        run_id = run.id

        result = await ClaudeCodeRuntime().execute(
            db,
            agent=agent,
            task_text="fix the bug",
            tenant_id=tenant,
            run_id=run_id,
        )

    assert result.status == "failed"


async def test_a_wedged_run_times_out_and_tears_down(
    app_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A container whose driver.wait() never returns (docker_driver.py's own
    "still running" signal, per its wait() docstring) must not poll forever:
    the run-level _RUN_TIMEOUT_S is the ONLY thing that bounds it, since
    every "still running" iteration raises out of driver.wait() rather than
    ever returning normally. Patches _RUN_TIMEOUT_S down to 0.05s so the
    test proves termination without waiting out the real 1800s default."""
    from runtime.runtime import ClaudeCodeRuntime

    driver = _NeverFinishingDriver(
        ['{"type":"system","subtype":"init","session_id":"claude-sess-3"}']
    )
    _install(monkeypatch, driver)
    monkeypatch.setattr("runtime.runtime._RUN_TIMEOUT_S", 0.05)

    tenant = uuid.uuid4()
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
        run_id = run.id

        started = time.monotonic()
        result = await ClaudeCodeRuntime().execute(
            db,
            agent=agent,
            task_text="fix the bug",
            tenant_id=tenant,
            run_id=run_id,
        )
        elapsed = time.monotonic() - started

    # Loose bound: proves the loop actually terminated (an un-fixed version
    # of this loop would hang here until the test runner's own timeout, if
    # any) rather than pinning to a tight number that flakes under load.
    assert elapsed < 5.0
    assert result.status == "failed"
    assert "timed out" in result.output
    assert driver.torn_down == 1


# --------------------------------------------------------------------------
# Findings from the final whole-branch review. Every one of these is mirrored,
# in the same shape, in codex_runtime's and opencode_runtime's own test files:
# the whole point of the review was that three near-identical plugins had
# drifted apart, so the tests that pin them have to stay identical too.
# --------------------------------------------------------------------------


async def _seed_agent(
    app_session: Any, tenant: uuid.UUID, context: dict[str, Any]
) -> tuple[Any, uuid.UUID]:
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
        run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state="running", context=context)
        db.add(run)
        await db.flush()
        return agent, run.id


async def test_the_session_directory_is_mounted_writable_so_a_resume_can_find_it(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--resume names a session whose transcript Claude Code writes to
    $CLAUDE_CONFIG_DIR/projects/..., i.e. OUTSIDE /workspace -- and teardown
    destroys the container filesystem at every park. So the flag alone proves
    nothing: without a persistent, writable HOST directory behind that path,
    a resumed run points at a session that no longer exists anywhere and the
    CLI errors out. This asserts the MOUNT, not the flag."""
    from runtime.runtime import _CLAUDE_CONFIG_DIR, ClaudeCodeRuntime

    driver = _FakeDriver(
        ['{"type":"result","subtype":"success","result":"Done.","session_id":"claude-sess-2"}']
    )
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(
        app_session, tenant, {"cli_harness_sessions": {"claude_code_runtime": "claude-sess-2"}}
    )
    async with app_session(tenant) as db:
        db.add(agent)
        await ClaudeCodeRuntime().execute(
            db, agent=agent, task_text="fix the bug", tenant_id=tenant, run_id=run_id
        )

    spec = driver.specs[0]
    assert "--resume" in spec.command
    home = next(mnt for mnt in spec.mounts if mnt.container_path == _CLAUDE_CONFIG_DIR)
    assert home.readonly is False, "a read-only config dir cannot hold a resumable transcript"
    assert home.host_path.startswith(str(tmp_path))
    # Per-RUN, and NOT nested inside the agent-writable /workspace mount.
    assert str(run_id) in home.host_path
    workspace = next(mnt for mnt in spec.mounts if mnt.container_path == "/workspace")
    assert not home.host_path.startswith(workspace.host_path + "/")
    # The container has to run as the uid that owns those host directories.
    assert hasattr(spec, "user")


async def test_every_mount_source_exists_on_disk_before_provision(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Docker auto-creates a missing bind-mount source as root:root, which an
    unprivileged container then cannot write to -- silently defeating both
    /workspace and the resume transcript. A FakeDriver whose provision()
    stats every mount source proves the ordering directly."""
    from runtime.runtime import ClaudeCodeRuntime

    seen: dict[str, bool] = {}

    class _OrderCheckingDriver(_FakeDriver):
        async def provision(self, spec: Any) -> SandboxHandle:
            seen["all_exist"] = all(
                # A local, cheap stat on a path this test just created.
                Path(mnt.host_path).exists()  # noqa: ASYNC240
                for mnt in spec.mounts
            )
            return await super().provision(spec)

    driver = _OrderCheckingDriver(
        ['{"type":"result","subtype":"success","result":"Done.","session_id":"s"}']
    )
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        await ClaudeCodeRuntime().execute(
            db, agent=agent, task_text="fix the bug", tenant_id=tenant, run_id=run_id
        )

    assert seen["all_exist"] is True


async def test_mounts_go_through_the_sandbox_allowlist(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """docker_driver.py documents sandbox.mounts.validate_mounts as a gate
    every caller must pass its bind-mount paths through, against ONE allowed
    root and by RESOLVED path. nanoclaw_runtime complies; this plugin did
    not."""
    import runtime.runtime as rt

    calls: list[str] = []
    real = rt.validate_mounts

    def _spy(mounts: Any, *, allowed_root: str) -> Any:
        calls.append(allowed_root)
        return real(mounts, allowed_root=allowed_root)

    monkeypatch.setattr(rt, "validate_mounts", _spy)
    driver = _FakeDriver(
        ['{"type":"result","subtype":"success","result":"Done.","session_id":"s"}']
    )
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        await rt.ClaudeCodeRuntime().execute(
            db, agent=agent, task_text="fix the bug", tenant_id=tenant, run_id=run_id
        )

    assert calls == [str(tmp_path)]


async def test_the_run_row_is_committed_before_the_container_is_polled(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """runtime/isolated.py and nanoclaw_runtime both commit immediately after
    provision(), and both name the deadlock it avoids: the /mcp gateway
    updates the same agent_run row from its OWN session on every tool call,
    and would otherwise block on this transaction's uncommitted RUNNING
    transition for the whole (possibly very long) container run -- while the
    executor waits on the container. Proven from OUTSIDE the executor's
    session: a second session must be able to see the task_id this run just
    opened, while execute() is still polling."""
    from runtime.runtime import ClaudeCodeRuntime

    seen: dict[str, Any] = {}

    class _ObservingDriver(_FakeDriver):
        async def logs(self, handle: SandboxHandle) -> str:
            if "task_id" not in seen:
                async with app_session(tenant) as other:
                    row = await other.get(m.AgentRun, run_id)
                    seen["task_id"] = row.task_id if row is not None else None
            return await super().logs(handle)

    driver = _ObservingDriver(
        ['{"type":"result","subtype":"success","result":"Done.","session_id":"s"}']
    )
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        await ClaudeCodeRuntime().execute(
            db, agent=agent, task_text="fix the bug", tenant_id=tenant, run_id=run_id
        )

    assert seen["task_id"] is not None, "the row lock was still held while the container ran"


async def test_a_real_task_row_is_opened_and_reused_on_resume(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`run.task_id or uuid.uuid4()` -- what every RunResult here used to
    carry -- fabricates an id no row has ever had. An approval raised against
    it can then never be resolved back to the run, so an approved action
    strands the run in waiting_for_approval forever. Mirrors what
    runtime/isolated.py does via open_run_task."""
    from runtime.runtime import ClaudeCodeRuntime

    driver = _FakeDriver(
        ['{"type":"result","subtype":"success","result":"Done.","session_id":"s"}']
    )
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        result = await ClaudeCodeRuntime().execute(
            db, agent=agent, task_text="fix the bug", tenant_id=tenant, run_id=run_id
        )

    async with app_session(tenant) as db:
        task = await db.get(m.Task, result.task_id)
        assert task is not None, "the RunResult carried a task id no row has"
        assert task.title == "fix the bug"
        run = await db.get(m.AgentRun, run_id)
        assert run is not None and run.task_id == result.task_id

    # A resume leg continues the SAME task rather than opening a second one.
    driver.specs.clear()
    async with app_session(tenant) as db:
        db.add(agent)
        again = await ClaudeCodeRuntime().execute(
            db, agent=agent, task_text="carry on", tenant_id=tenant, run_id=run_id
        )
    assert again.task_id == result.task_id


async def test_an_operator_cancel_interrupts_the_poll_loop(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§7.2. nanoclaw_runtime probes cancel_check every poll iteration; this
    plugin accepted the parameter and never called it, so a runaway agent
    could not be stopped at all -- the one failure mode an operator has no
    other answer to."""
    from runtime.runtime import ClaudeCodeRuntime

    driver = _NeverFinishingDriver(
        ['{"type":"system","subtype":"init","session_id":"claude-sess-4"}']
    )
    _install(monkeypatch, driver)

    async def _cancelled() -> bool:
        return True

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        started = time.monotonic()
        result = await ClaudeCodeRuntime().execute(
            db,
            agent=agent,
            task_text="fix the bug",
            tenant_id=tenant,
            run_id=run_id,
            cancel_check=_cancelled,
        )
        elapsed = time.monotonic() - started

    assert result.status == "interrupted"
    assert elapsed < 5.0, "cancel must not wait out the run-level timeout"
    assert driver.torn_down == 1


async def test_a_clean_exit_with_no_terminal_event_says_so(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """docker_driver.logs() returns a 4000-char TRAILING window, so a chatty
    run can lose its one terminal `result` line off the front between polls --
    and done-vs-failed is decided entirely on having seen it. That is not the
    same event as "the task failed", and must not read like it in production
    logs."""
    from runtime.runtime import ClaudeCodeRuntime

    driver = _FakeDriver(['{"type":"assistant","message":{"content":"working..."}}'], exit_code=0)
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        result = await ClaudeCodeRuntime().execute(
            db, agent=agent, task_text="fix the bug", tenant_id=tenant, run_id=run_id
        )

    assert result.status == "failed"
    assert "no terminal event" in result.output
    assert "log window" in result.output or "trailing" in result.output


async def test_task_text_cannot_be_parsed_as_a_flag(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`claude`'s `-p` is `--print`, a BOOLEAN flag -- it does not consume the
    next token as its value, so task_text is a bare positional exactly like
    codex_runtime's and opencode_runtime's. MEASURED against claude 2.1.229:
    `claude -p --bare --output-format json --nonexistent-flag-xyz` exits with
    "error: unknown option", while the same command with `--` before it runs
    it as the prompt. Covers both the fresh-leg and resume-leg shapes."""
    from runtime.runtime import ClaudeCodeRuntime

    hostile = "--dangerously-skip-permissions"
    driver = _FakeDriver(
        ['{"type":"result","subtype":"success","result":"Done.","session_id":"s"}']
    )
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        await ClaudeCodeRuntime().execute(
            db, agent=agent, task_text=hostile, tenant_id=tenant, run_id=run_id
        )
    assert driver.specs[0].command[-2:] == ["--", f"{hostile}\n\n{TOOLCHAIN_NOTE}"]

    driver.specs.clear()
    agent2, run_id2 = await _seed_agent(
        app_session, tenant, {"cli_harness_sessions": {"claude_code_runtime": "claude-sess-9"}}
    )
    async with app_session(tenant) as db:
        db.add(agent2)
        await ClaudeCodeRuntime().execute(
            db, agent=agent2, task_text=hostile, tenant_id=tenant, run_id=run_id2
        )
    resume_spec = driver.specs[0]
    assert resume_spec.command[-2:] == ["--", f"{hostile}\n\n{TOOLCHAIN_NOTE}"]
    assert "claude-sess-9" in resume_spec.command


async def test_the_command_is_fully_autonomous_and_not_token_streaming(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two separate findings, both visible in the same argv.

    --permission-mode: `acceptEdits` auto-accepts file EDITS only; Bash and
    every other tool still raise a permission request, and headless -p mode
    has nobody to answer one, so they are denied outright. That contradicts
    codex_runtime's approval_policy="never" and opencode_runtime's --auto,
    which are this plan's actual design decision (native tools allowed,
    sandboxed by the container).

    --include-partial-messages: emits a JSON event per token delta, which is
    what pushes the terminal `result` line off the front of the 4000-char log
    window before the poll loop can see it. Nothing here consumes a delta.
    --verbose stays: the CLI REFUSES stream-json without it."""
    from runtime.runtime import ClaudeCodeRuntime

    driver = _FakeDriver(
        ['{"type":"result","subtype":"success","result":"Done.","session_id":"s"}']
    )
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        await ClaudeCodeRuntime().execute(
            db, agent=agent, task_text="fix the bug", tenant_id=tenant, run_id=run_id
        )

    cmd = driver.specs[0].command
    mode = cmd.index("--permission-mode")
    assert cmd[mode + 1] == "bypassPermissions"
    assert "--include-partial-messages" not in cmd
    assert "--verbose" in cmd


async def test_the_run_token_is_never_put_on_the_command_line(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--mcp-config takes "JSON files or strings"; this plugin passes a FILE.

    Passing the string put the run's bearer JWT straight into the container's
    argv, readable by any `docker inspect` and out of /proc/<pid>/cmdline --
    a live credential. codex_runtime (bearer_token_env_var) and
    opencode_runtime (a config file) had both already closed this by
    construction; this is the same close for the third plugin.

    The file is mounted READ-ONLY and at a path NOT nested inside the
    read-write $CLAUDE_CONFIG_DIR, so the agent's own file-edit tools cannot
    rewrite the endpoint it is governed through."""
    import json as _json

    from runtime.runtime import (
        _CLAUDE_CONFIG_DIR,
        _MCP_CONFIG_PATH,
        ClaudeCodeRuntime,
    )

    driver = _FakeDriver(
        ['{"type":"result","subtype":"success","result":"Done.","session_id":"s"}']
    )
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        await ClaudeCodeRuntime().execute(
            db, agent=agent, task_text="fix the bug", tenant_id=tenant, run_id=run_id
        )

    spec = driver.specs[0]
    cfg = spec.command[spec.command.index("--mcp-config") + 1]
    assert cfg == _MCP_CONFIG_PATH, "the flag must name a path, not carry the JSON"

    token = spec.env["ANTHROPIC_AUTH_TOKEN"]
    assert token, "sanity: the run really does mint a token"
    assert not any(token in arg for arg in spec.command), "the JWT is exposed via docker inspect"

    mount = next(mnt for mnt in spec.mounts if mnt.container_path == _MCP_CONFIG_PATH)
    assert mount.readonly is True, "the agent could otherwise rewrite its own MCP endpoint"
    assert not _MCP_CONFIG_PATH.startswith(_CLAUDE_CONFIG_DIR + "/"), (
        "must not sit inside the read-WRITE config dir, or the read-only mount is pointless"
    )

    # The file really is on disk before provision(), and really does carry the
    # token -- otherwise the container gets no MCP server at all.
    written = _json.loads(Path(mount.host_path).read_text(encoding="utf-8"))  # noqa: ASYNC240
    assert written["mcpServers"]["oc8"]["headers"]["Authorization"] == f"Bearer {token}"
    # 0600: it outlives the container on the host.
    assert oct(Path(mount.host_path).stat().st_mode)[-3:] == "600"  # noqa: ASYNC240


async def test_the_toolchain_note_is_appended_to_the_task_prompt(
    app_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime.runtime import ClaudeCodeRuntime

    result_event = (
        '{"type":"result","subtype":"success","result":"done","session_id":"claude-sess-1"}'
    )
    driver = _FakeDriver([result_event], exit_code=0)
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
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
        run_id = run.id

        await ClaudeCodeRuntime().execute(
            db, agent=agent, task_text="fix the bug", tenant_id=tenant, run_id=run_id,
        )

    spec = driver.specs[0]
    assert spec.command[-1].startswith("fix the bug\n\n")
    assert "Playwright" in spec.command[-1]
