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

# tests/plugins/codex_runtime/test_runtime.py -> repo root -> the plugin folder.
# This plugin is NOT on pytest's `pythonpath` any more (see backend/pyproject.toml):
# every plugin resolves its own package from its own test module, because after the
# package restructure four plugins share the folder name `runtime/` and a blanket path
# list would silently hand `import runtime` to whichever root sorts first.
PLUGIN_ROOT = Path(__file__).resolve().parents[4] / "capas" / "codex_runtime"


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

    Every `monkeypatch.setattr("runtime.runtime.…")` in this file sits inside a
    test body, so it runs after this fixture and resolves against the module this
    fixture just made current. A file whose string-patching happens in another AUTOUSE
    fixture instead must make that fixture take `_plugin_path` as an argument (see
    tests/plugins/claude_code_runtime/test_runtime.py) -- declaration order is not an
    ordering guarantee.

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


class _FakeDriver:
    def __init__(self, stdout_lines: list[str], *, exit_code: int = 0) -> None:
        self.stdout_lines = stdout_lines
        self.exit_code = exit_code
        self.specs: list[Any] = []
        self.fs_writes: dict[str, bytes] = {}
        self.torn_down = 0
        #: What get_archive hands back for $CODEX_HOME at the end of the leg.
        self.home: dict[str, bytes] = {}
        self.archived: list[str] = []

    async def get_archive(
        self, handle: SandboxHandle, path: str, *, max_bytes: int | None = None
    ) -> bytes:
        self.archived.append(path)
        return _home_tar(self.home)

    async def provision(self, spec: Any) -> SandboxHandle:
        self.specs.append(spec)
        return SandboxHandle(container_id="codex-container", image=spec.image)

    async def logs(self, handle: SandboxHandle) -> str:
        return "".join(line + "\n" for line in self.stdout_lines)

    async def wait(self, handle: SandboxHandle, timeout_s: float = 600.0) -> int:
        return self.exit_code

    async def exec(self, handle: SandboxHandle, command: list[str], **kw: Any) -> ExecResult:
        # The output sync's listing: no /workspace/output files.
        return ExecResult(exit_code=0, output="uid=1000\0")

    async def fs_write(self, handle: SandboxHandle, path: str, content: bytes) -> None:
        self.fs_writes[path] = content

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
    `codex` CLI process that never exits, to prove the run-level timeout --
    not driver.wait()'s own short poll timeout -- is what eventually ends
    the run. Mirrors claude_code_runtime's own _NeverFinishingDriver
    (backend/tests/plugins/claude_code_runtime/test_runtime.py)."""

    async def wait(self, handle: SandboxHandle, timeout_s: float = 600.0) -> int:
        raise TimeoutError("still running")


def _install(monkeypatch: pytest.MonkeyPatch, driver: Any) -> None:
    monkeypatch.setattr("runtime.runtime.get_sandbox_driver", lambda: driver)


@pytest.fixture(autouse=True)
def _storage(_plugin_path: None, minio_url: str) -> None:
    """Every leg saves $CODEX_HOME to RunStateStore (object storage) and a
    resume leg loads it back, so every test needs storage. Takes
    `_plugin_path` for the ordering only."""


def _home_tar(files: dict[str, bytes]) -> bytes:
    from oc8.runtime.run_state import pack_files

    return pack_files(files)


async def _save_home(tenant: uuid.UUID, run_id: uuid.UUID, files: dict[str, bytes]) -> None:
    """What leg 1 left behind: its $CODEX_HOME, as RunStateStore keeps it."""
    from oc8.runtime.run_state import RunStateStore

    await RunStateStore(tenant).save(run_id, "codex-home", _home_tar(files))


def _seed_members(spec: Any) -> dict[str, tuple[bytes | None, int, int, int]]:
    """name -> (content or None for a dir, uid, gid, mode) of a spec's seed."""
    import io
    import tarfile

    assert spec.seed is not None, "the spec carries no seed"
    out: dict[str, tuple[bytes | None, int, int, int]] = {}
    with tarfile.open(fileobj=io.BytesIO(spec.seed), mode="r:") as tar:
        for member in tar.getmembers():
            f = tar.extractfile(member) if member.isfile() else None
            out[member.name] = (f.read() if f else None, member.uid, member.gid, member.mode)
    return out


_ROLLOUT = "sessions/2026/10/22/rollout-2026-10-22T10-00-00-codex-sess-1.jsonl"


async def _seed_agent(app_session: Any, tenant: uuid.UUID, context: dict) -> tuple[Any, uuid.UUID]:
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


async def test_a_finished_run_returns_done(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from runtime.runtime import CodexRuntime

    driver = _FakeDriver(
        [
            '{"type":"thread.started","thread_id":"codex-sess-1"}',
            '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"Fixed it."}}',
        ],
        exit_code=0,
    )
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        result = await CodexRuntime().execute(
            db,
            agent=agent,
            task_text="fix the bug",
            tenant_id=tenant,
            run_id=run_id,
        )

    assert result.status == "done"
    assert result.output == "Fixed it."
    assert driver.torn_down == 1
    spec = driver.specs[0]
    from oc8.sandbox.reaper import TENANT_LABEL

    assert spec.labels[TENANT_LABEL] == str(tenant)
    assert spec.command[:2] == ["codex", "exec"]
    assert "resume" not in spec.command


async def test_config_toml_disables_interactive_approval(
    app_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """config.toml travels in the seed, at its in-container path, owned by the
    image's uid-1000 user. It names the env var holding the token rather
    than carrying the token itself."""
    from runtime.runtime import CodexRuntime

    driver = _FakeDriver(['{"type":"thread.started","thread_id":"s1"}'])
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        await CodexRuntime().execute(
            db,
            agent=agent,
            task_text="fix the bug",
            tenant_id=tenant,
            run_id=run_id,
        )

    spec = driver.specs[0]
    content, uid, gid, mode = _seed_members(spec)["home/node/.codex/config.toml"]
    assert content is not None
    text = content.decode()
    assert 'approval_policy = "never"' in text
    assert 'sandbox_mode = "workspace-write"' in text
    assert "[mcp_servers.oc8]" in text
    assert 'type = "streamable_http"' in text
    assert 'bearer_token_env_var = "OC8_TOKEN"' in text
    assert spec.env["OC8_TOKEN"] and spec.env["OC8_TOKEN"] not in text
    assert (uid, gid, mode) == (1000, 1000, 0o600)


async def test_config_toml_is_in_the_spec_the_container_is_provisioned_with(
    app_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race the seed closes: config.toml must be in place before Codex's
    entrypoint could read it, not written by a second, unsynchronized call
    after provision(). It is part of the very spec provision() receives
    (drivers unpack the seed before start), and nothing is written after."""
    from runtime.runtime import CodexRuntime

    seen_at_provision: dict[str, bool] = {}

    class _OrderCheckingDriver(_FakeDriver):
        async def provision(self, spec: Any) -> SandboxHandle:
            seen_at_provision["seeded"] = "home/node/.codex/config.toml" in _seed_members(spec)
            return await super().provision(spec)

    driver = _OrderCheckingDriver(['{"type":"thread.started","thread_id":"s1"}'])
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        await CodexRuntime().execute(
            db,
            agent=agent,
            task_text="fix the bug",
            tenant_id=tenant,
            run_id=run_id,
        )

    assert seen_at_provision["seeded"] is True
    assert driver.fs_writes == {}


async def test_a_resume_leg_uses_the_resume_subcommand(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from runtime.runtime import CodexRuntime

    driver = _FakeDriver(
        ['{"type":"item.completed","item":{"type":"agent_message","text":"Done."}}']
    )
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(
        app_session, tenant, {"cli_harness_sessions": {"codex_runtime": "codex-sess-1"}}
    )
    await _save_home(tenant, run_id, {_ROLLOUT: b"{}\n"})
    async with app_session(tenant) as db:
        db.add(agent)
        await CodexRuntime().execute(
            db,
            agent=agent,
            task_text="fix the bug",
            tenant_id=tenant,
            run_id=run_id,
        )

    spec = driver.specs[0]
    assert spec.command[:3] == ["codex", "exec", "resume"]
    assert "codex-sess-1" in spec.command


async def test_task_text_cannot_be_parsed_as_a_flag(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A task_text that happens to start with `-` (e.g. an agent's own task
    description echoing back a literal Codex flag) must never reach codex's
    clap-based parser as anything but the prompt positional. Without the `--`
    end-of-flags separator immediately before it, clap would parse
    "--dangerously-bypass-approvals-and-sandbox" as a FLAG -- defeating the
    approval_policy/sandbox_mode config.toml this plugin relies on to keep
    Codex non-interactive and sandboxed. Covers both the fresh-leg and
    resume-leg command shapes."""
    from runtime.runtime import CodexRuntime

    hostile_task_text = "--dangerously-bypass-approvals-and-sandbox"

    driver = _FakeDriver(
        ['{"type":"item.completed","item":{"type":"agent_message","text":"Done."}}']
    )
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        await CodexRuntime().execute(
            db,
            agent=agent,
            task_text=hostile_task_text,
            tenant_id=tenant,
            run_id=run_id,
        )

    fresh_spec = driver.specs[0]
    assert fresh_spec.command[-2:] == ["--", f"{hostile_task_text}\n\n{TOOLCHAIN_NOTE}"]

    driver.specs.clear()
    agent2, run_id2 = await _seed_agent(
        app_session, tenant, {"cli_harness_sessions": {"codex_runtime": "codex-sess-9"}}
    )
    await _save_home(tenant, run_id2, {_ROLLOUT: b"{}\n"})
    async with app_session(tenant) as db:
        db.add(agent2)
        await CodexRuntime().execute(
            db,
            agent=agent2,
            task_text=hostile_task_text,
            tenant_id=tenant,
            run_id=run_id2,
        )

    resume_spec = driver.specs[0]
    assert resume_spec.command[-2:] == ["--", f"{hostile_task_text}\n\n{TOOLCHAIN_NOTE}"]
    assert "codex-sess-9" in resume_spec.command


async def test_a_wedged_run_times_out_and_tears_down(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A container whose driver.wait() never returns (docker_driver.py's own
    "still running" signal, per its wait() docstring) must not poll forever:
    the run-level _RUN_TIMEOUT_S is the ONLY thing that bounds it, since
    every "still running" iteration raises out of driver.wait() rather than
    ever returning normally. Patches _RUN_TIMEOUT_S down to 0.05s so the
    test proves termination without waiting out the real 1800s default.
    Mirrors claude_code_runtime's own test_a_wedged_run_times_out_and_tears_down
    exactly in shape."""
    from runtime.runtime import CodexRuntime

    driver = _NeverFinishingDriver(['{"type":"thread.started","thread_id":"codex-sess-3"}'])
    _install(monkeypatch, driver)
    monkeypatch.setattr("runtime.runtime._RUN_TIMEOUT_S", 0.05)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        started = time.monotonic()
        result = await CodexRuntime().execute(
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
# in the same shape, in claude_code_runtime's and opencode_runtime's own test
# files: the whole point of the review was that three near-identical plugins
# had drifted apart, so the tests that pin them have to stay identical too.
# --------------------------------------------------------------------------


async def test_the_sandbox_has_scratch_dirs_and_no_mounts(
    app_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No host directory exists behind a sandbox any more: /workspace and
    $CODEX_HOME are scratch dirs the driver provides, and the CLI only
    reaches the backend."""
    from runtime.runtime import _CODEX_HOME, CodexRuntime

    driver = _FakeDriver(
        ['{"type":"item.completed","item":{"type":"agent_message","text":"Done."}}']
    )
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        await CodexRuntime().execute(
            db, agent=agent, task_text="fix the bug", tenant_id=tenant, run_id=run_id
        )

    spec = driver.specs[0]
    assert spec.mounts == []
    assert {d.path for d in spec.scratch} == {"/workspace", _CODEX_HOME}
    assert all(d.size_mb > 0 for d in spec.scratch)
    assert spec.egress == "restricted"
    assert spec.workdir == "/workspace"
    assert spec.env["CODEX_HOME"] == _CODEX_HOME


async def test_a_resume_leg_restores_the_session_leg_one_saved(
    app_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`codex exec resume <id>` reads the rollout .jsonl Codex writes under
    $CODEX_HOME/sessions/, OUTSIDE /workspace, and teardown destroys the
    sandbox at every park. Leg 1 saves $CODEX_HOME (without its config,
    which every leg seeds fresh); leg 2's seed puts the rollout back and
    resumes it by its explicit id, never `--last`."""
    from runtime.runtime import _CODEX_HOME, CodexRuntime

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(
        app_session,
        tenant,
        {"isolated_result": {"status": "waiting_for_approval", "output": "send it?"}},
    )

    leg1 = _FakeDriver(['{"type":"thread.started","thread_id":"codex-sess-1"}'])
    leg1.home = {
        _ROLLOUT: b'{"type": "session_meta"}\n',
        "config.toml": b'approval_policy = "on-request"\n',
        "log/codex-tui.log": b"noise",
    }
    _install(monkeypatch, leg1)
    async with app_session(tenant) as db:
        db.add(agent)
        first = await CodexRuntime().execute(
            db, agent=agent, task_text="send the report", tenant_id=tenant, run_id=run_id
        )
    assert first.status == "waiting_for_approval"
    assert leg1.archived == [_CODEX_HOME]

    async with app_session(tenant) as db:
        from oc8.runtime.run_context import merge_context

        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        await merge_context(db, run, {"isolated_result": {}})
    leg2 = _FakeDriver(['{"type":"item.completed","item":{"type":"agent_message","text":"Sent."}}'])
    _install(monkeypatch, leg2)
    async with app_session(tenant) as db:
        db.add(agent)
        second = await CodexRuntime().execute(
            db, agent=agent, task_text="approved", tenant_id=tenant, run_id=run_id
        )
    assert second.status == "done"

    spec = leg2.specs[0]
    assert spec.command[:4] == ["codex", "exec", "resume", "codex-sess-1"]
    assert "--last" not in spec.command
    seeded = _seed_members(spec)
    home = _CODEX_HOME.lstrip("/")
    assert seeded[f"{home}/{_ROLLOUT}"][0] == b'{"type": "session_meta"}\n'
    # The agent's edit of its config did not outlive leg 1, and logs stay out.
    config = seeded[f"{home}/config.toml"][0]
    assert config is not None and b'approval_policy = "never"' in config
    assert f"{home}/log/codex-tui.log" not in seeded


async def test_a_resume_leg_without_saved_state_fails_before_any_sandbox(
    app_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime.runtime import CodexRuntime

    driver = _FakeDriver([])
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(
        app_session, tenant, {"cli_harness_sessions": {"codex_runtime": "codex-sess-7"}}
    )
    async with app_session(tenant) as db:
        db.add(agent)
        result = await CodexRuntime().execute(
            db, agent=agent, task_text="carry on", tenant_id=tenant, run_id=run_id
        )

    assert result.status == "failed"
    assert "codex-sess-7" in result.output and "missing" in result.output
    assert driver.specs == []


async def test_the_run_row_is_committed_before_the_container_is_polled(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """runtime/isolated.py and nanoclaw_runtime both commit immediately after
    provision(), and both name the deadlock it avoids: the /mcp gateway
    updates the same agent_run row from its OWN session on every tool call,
    and would otherwise block on this transaction's uncommitted RUNNING
    transition for the whole container run."""
    from runtime.runtime import CodexRuntime

    seen: dict[str, Any] = {}

    class _ObservingDriver(_FakeDriver):
        async def logs(self, handle: SandboxHandle) -> str:
            if "task_id" not in seen:
                async with app_session(tenant) as other:
                    row = await other.get(m.AgentRun, run_id)
                    seen["task_id"] = row.task_id if row is not None else None
            return await super().logs(handle)

    driver = _ObservingDriver(
        ['{"type":"item.completed","item":{"type":"agent_message","text":"Done."}}']
    )
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        await CodexRuntime().execute(
            db, agent=agent, task_text="fix the bug", tenant_id=tenant, run_id=run_id
        )

    assert seen["task_id"] is not None, "the row lock was still held while the container ran"


async def test_a_real_task_row_is_opened_and_reused_on_resume(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`run.task_id or uuid.uuid4()` -- what every RunResult here used to
    carry -- fabricates an id no row has ever had, so an approval raised
    against it can never be resolved back to the run."""
    from runtime.runtime import CodexRuntime

    driver = _FakeDriver(
        ['{"type":"item.completed","item":{"type":"agent_message","text":"Done."}}']
    )
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        result = await CodexRuntime().execute(
            db, agent=agent, task_text="fix the bug", tenant_id=tenant, run_id=run_id
        )

    async with app_session(tenant) as db:
        task = await db.get(m.Task, result.task_id)
        assert task is not None, "the RunResult carried a task id no row has"
        assert task.title == "fix the bug"
        run = await db.get(m.AgentRun, run_id)
        assert run is not None and run.task_id == result.task_id

    driver.specs.clear()
    async with app_session(tenant) as db:
        db.add(agent)
        again = await CodexRuntime().execute(
            db, agent=agent, task_text="carry on", tenant_id=tenant, run_id=run_id
        )
    assert again.task_id == result.task_id


async def test_an_operator_cancel_interrupts_the_poll_loop(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§7.2. nanoclaw_runtime probes cancel_check every poll iteration; this
    plugin accepted the parameter and never called it."""
    from runtime.runtime import CodexRuntime

    driver = _NeverFinishingDriver(['{"type":"thread.started","thread_id":"codex-sess-4"}'])
    _install(monkeypatch, driver)

    async def _cancelled() -> bool:
        return True

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        started = time.monotonic()
        result = await CodexRuntime().execute(
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
    run can lose its one terminal agent_message off the front between polls --
    and done-vs-failed is decided entirely on having seen it. That is not the
    same event as "the task failed"."""
    from runtime.runtime import CodexRuntime

    driver = _FakeDriver(['{"type":"turn.started"}'], exit_code=0)
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        result = await CodexRuntime().execute(
            db, agent=agent, task_text="fix the bug", tenant_id=tenant, run_id=run_id
        )

    assert result.status == "failed"
    assert "no terminal event" in result.output


async def test_codex_is_allowed_to_run_outside_a_git_repo(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """/workspace is a bare bind mount, and `codex exec` refuses outright in a
    directory that is not a git repo: "Not inside a trusted directory and
    --skip-git-repo-check was not specified", exit 2 -- measured against
    codex-cli 0.147.0 inside this plugin's own image. Without this flag every
    single run of this runtime fails before it starts. Both leg shapes."""
    from runtime.runtime import CodexRuntime

    driver = _FakeDriver(
        ['{"type":"item.completed","item":{"type":"agent_message","text":"Done."}}']
    )
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        await CodexRuntime().execute(
            db, agent=agent, task_text="fix the bug", tenant_id=tenant, run_id=run_id
        )
    assert "--skip-git-repo-check" in driver.specs[0].command

    driver.specs.clear()
    agent2, run_id2 = await _seed_agent(
        app_session, tenant, {"cli_harness_sessions": {"codex_runtime": "codex-sess-8"}}
    )
    await _save_home(tenant, run_id2, {_ROLLOUT: b"{}\n"})
    async with app_session(tenant) as db:
        db.add(agent2)
        await CodexRuntime().execute(
            db, agent=agent2, task_text="carry on", tenant_id=tenant, run_id=run_id2
        )
    assert "--skip-git-repo-check" in driver.specs[0].command


async def test_the_toolchain_note_is_appended_to_the_task_prompt(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from runtime.runtime import CodexRuntime

    driver = _FakeDriver(
        [
            '{"type":"thread.started","thread_id":"codex-sess-1"}',
            '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
        ],
        exit_code=0,
    )
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        await CodexRuntime().execute(
            db,
            agent=agent,
            task_text="fix the bug",
            tenant_id=tenant,
            run_id=run_id,
        )

    spec = driver.specs[0]
    assert spec.command[-1].startswith("fix the bug\n\n")
    assert "Playwright" in spec.command[-1]
