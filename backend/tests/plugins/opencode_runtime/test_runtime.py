from __future__ import annotations

import json
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

# tests/plugins/opencode_runtime/test_runtime.py -> repo root -> the plugin folder.
# This plugin is NOT on pytest's `pythonpath` any more (see backend/pyproject.toml):
# every plugin resolves its own package from its own test module, because after the
# package restructure four plugins share the folder name `runtime/` and a blanket path
# list would silently hand `import runtime` to whichever root sorts first.
PLUGIN_ROOT = Path(__file__).resolve().parents[4] / "capas" / "opencode_runtime"


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
    ~18 plugin imports sits INSIDE a test body and resolves at execution time, long
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


class _FakeDriver:
    def __init__(self, stdout_lines: list[str], *, exit_code: int = 0) -> None:
        self.stdout_lines = stdout_lines
        self.exit_code = exit_code
        self.specs: list[Any] = []
        self.fs_writes: dict[str, bytes] = {}
        self.torn_down = 0

    async def provision(self, spec: Any) -> SandboxHandle:
        self.specs.append(spec)
        return SandboxHandle(container_id="opencode-container", image=spec.image)

    async def logs(self, handle: SandboxHandle) -> str:
        return "".join(line + "\n" for line in self.stdout_lines)

    async def wait(self, handle: SandboxHandle, timeout_s: float = 600.0) -> int:
        return self.exit_code

    async def exec(self, handle: SandboxHandle, command: list[str], **kw: Any) -> ExecResult:
        raise NotImplementedError

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
    `opencode` CLI process that never exits, to prove the run-level timeout
    -- not driver.wait()'s own short poll timeout -- is what eventually
    ends the run. Mirrors claude_code_runtime's and codex_runtime's own
    _NeverFinishingDriver."""

    async def wait(self, handle: SandboxHandle, timeout_s: float = 600.0) -> int:
        raise TimeoutError("still running")


def _install(monkeypatch: pytest.MonkeyPatch, driver: Any) -> None:
    monkeypatch.setattr("runtime.runtime.get_sandbox_driver", lambda: driver)


def _use_tmp_session_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The opencode.jsonc race fix (see runtime.py's _write_opencode_config
    docstring) means execute() now does REAL disk I/O -- os.makedirs() and a
    real file write -- even against a FakeDriver, since the whole point of
    the fix is that the file exists on the host BEFORE driver.provision() is
    ever called. The real runtime_session_root default
    (/var/lib/oc8/sessions) is neither writable nor expected to exist outside
    a deployed container, so every test must redirect it to a pytest tmp_path
    the same way codex_runtime's (and nanoclaw_runtime's) own tests redirect
    their SESSION_ROOT_OVERRIDE."""
    monkeypatch.setattr("runtime.runtime.SESSION_ROOT_OVERRIDE", str(tmp_path))


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
    from runtime.runtime import OpencodeRuntime

    _use_tmp_session_root(monkeypatch, tmp_path)
    driver = _FakeDriver(
        [
            '{"type":"tool_use","sessionID":"opencode-sess-1",'
            '"part":{"type":"tool","state":{"status":"completed"}}}',
            '{"type":"text","sessionID":"opencode-sess-1",'
            '"part":{"type":"text","text":"Fixed it.","time":{"end":123456}}}',
        ],
    )
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        result = await OpencodeRuntime().execute(
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
    assert spec.command[:2] == ["opencode", "run"]
    assert "--auto" in spec.command
    assert "--format" in spec.command and "json" in spec.command


async def test_config_points_the_custom_provider_at_oc8s_gateway(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Also proves the write-before-provision fix: opencode.jsonc must
    already exist on the host (mounted in via a BindMount) by the time
    provision() runs, NOT be pushed into the container after the fact via
    driver.fs_write() -- see runtime.py's _write_opencode_config docstring
    for the race that fix closes. This test reads the config from the mount
    entry's host_path on disk, rather than from driver.fs_writes, precisely
    because fs_write is no longer called at all for the config file."""
    from runtime.runtime import OpencodeRuntime

    _use_tmp_session_root(monkeypatch, tmp_path)
    driver = _FakeDriver(
        ['{"type":"text","sessionID":"s1","part":{"type":"text","text":"ok","time":{"end":1}}}']
    )
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        await OpencodeRuntime().execute(
            db,
            agent=agent,
            task_text="fix the bug",
            tenant_id=tenant,
            run_id=run_id,
        )

    spec = driver.specs[0]
    mount = next(mnt for mnt in spec.mounts if mnt.container_path.endswith("opencode.jsonc"))
    assert mount.readonly is True
    # NOT nested under /workspace -- see _write_opencode_config's docstring
    # for why the config must be a sibling, unreachable via the agent's
    # writable /workspace mount.
    assert not mount.container_path.startswith("/workspace")
    # A local, cheap read of a tiny file this same test just wrote via
    # execute() -- not the kind of blocking call asyncio.to_thread exists to
    # protect against; mirrors codex_runtime's identical suppression.
    config = json.loads(Path(mount.host_path).read_text(encoding="utf-8"))  # noqa: ASYNC240
    assert config["provider"]["oc8"]["npm"] == "@ai-sdk/openai-compatible"
    assert config["provider"]["oc8"]["options"]["baseURL"].endswith("/llm/v1")
    assert config["mcp"]["oc8"]["type"] == "remote"
    assert config["mcp"]["oc8"]["headers"]["Authorization"].startswith("Bearer ")


async def test_opencode_config_is_written_before_the_container_is_provisioned(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The race this whole fix exists to close: opencode.jsonc must be a real
    file on disk by the time driver.provision() is called (and therefore by
    the time opencode's entrypoint could possibly read it), not written via a
    second, unsynchronized call afterward. A FakeDriver whose provision()
    checks for the file's existence proves the ordering directly, rather
    than just trusting that no post-provision fs_write call remains."""
    from runtime.runtime import OpencodeRuntime

    _use_tmp_session_root(monkeypatch, tmp_path)

    seen_at_provision: dict[str, bool] = {}

    class _OrderCheckingDriver(_FakeDriver):
        async def provision(self, spec: Any) -> SandboxHandle:
            mount = next(
                mnt for mnt in spec.mounts if mnt.container_path.endswith("opencode.jsonc")
            )
            # Same rationale as above: a local, cheap stat.
            seen_at_provision["exists"] = Path(mount.host_path).is_file()  # noqa: ASYNC240
            return await super().provision(spec)

    driver = _OrderCheckingDriver(
        ['{"type":"text","sessionID":"s1","part":{"type":"text","text":"ok","time":{"end":1}}}']
    )
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        await OpencodeRuntime().execute(
            db,
            agent=agent,
            task_text="fix the bug",
            tenant_id=tenant,
            run_id=run_id,
        )

    assert seen_at_provision["exists"] is True


async def test_a_resume_leg_passes_the_session_flag(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from runtime.runtime import OpencodeRuntime

    _use_tmp_session_root(monkeypatch, tmp_path)
    driver = _FakeDriver(
        [
            '{"type":"text","sessionID":"opencode-sess-1",'
            '"part":{"type":"text","text":"Done.","time":{"end":1}}}'
        ]
    )
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(
        app_session, tenant, {"cli_harness_sessions": {"opencode_runtime": "opencode-sess-1"}}
    )
    async with app_session(tenant) as db:
        db.add(agent)
        await OpencodeRuntime().execute(
            db,
            agent=agent,
            task_text="fix the bug",
            tenant_id=tenant,
            run_id=run_id,
        )

    spec = driver.specs[0]
    assert "--session" in spec.command
    session_index = spec.command.index("--session")
    assert spec.command[session_index + 1] == "opencode-sess-1"


async def test_task_text_cannot_be_parsed_as_a_flag(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A task_text that happens to start with `-` (e.g. an agent's own task
    description echoing back a literal opencode flag) must never reach
    opencode's parser as anything but the prompt positional. Without the
    `--` end-of-flags separator immediately before it, a task_text like
    "--auto" could parse as a FLAG instead of the message. Covers both the
    fresh-leg and resume-leg command shapes, and position-pins the exact
    trailing slice rather than just checking "--" is present somewhere,
    matching codex_runtime's own test_task_text_cannot_be_parsed_as_a_flag."""
    from runtime.runtime import OpencodeRuntime

    _use_tmp_session_root(monkeypatch, tmp_path)
    hostile_task_text = "--dangerous-looking-flag"

    driver = _FakeDriver(
        ['{"type":"text","sessionID":"s1","part":{"type":"text","text":"Done.","time":{"end":1}}}']
    )
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        await OpencodeRuntime().execute(
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
        app_session, tenant, {"cli_harness_sessions": {"opencode_runtime": "opencode-sess-9"}}
    )
    async with app_session(tenant) as db:
        db.add(agent2)
        await OpencodeRuntime().execute(
            db,
            agent=agent2,
            task_text=hostile_task_text,
            tenant_id=tenant,
            run_id=run_id2,
        )

    resume_spec = driver.specs[0]
    assert resume_spec.command[-2:] == ["--", f"{hostile_task_text}\n\n{TOOLCHAIN_NOTE}"]
    assert "opencode-sess-9" in resume_spec.command


async def test_a_wedged_run_times_out_and_tears_down(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A container whose driver.wait() never returns (docker_driver.py's own
    "still running" signal, per its wait() docstring) must not poll forever:
    the run-level _RUN_TIMEOUT_S is the ONLY thing that bounds it, since
    every "still running" iteration raises out of driver.wait() rather than
    ever returning normally. Patches _RUN_TIMEOUT_S down to 0.05s so the
    test proves termination without waiting out the real 1800s default.
    Mirrors claude_code_runtime's and codex_runtime's own
    test_a_wedged_run_times_out_and_tears_down exactly in shape."""
    from runtime.runtime import OpencodeRuntime

    _use_tmp_session_root(monkeypatch, tmp_path)
    driver = _NeverFinishingDriver(
        [
            '{"type":"text","sessionID":"opencode-sess-3",'
            '"part":{"type":"text","text":"working...","time":{"end":1}}}'
        ]
    )
    _install(monkeypatch, driver)
    monkeypatch.setattr("runtime.runtime._RUN_TIMEOUT_S", 0.05)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        started = time.monotonic()
        result = await OpencodeRuntime().execute(
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


async def test_multiple_text_events_are_concatenated_in_emission_order(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """opencode's real --format json output can emit several "text"-typed
    events in one turn (multiple text segments), not one single terminal
    "message.completed" event -- see runtime.py's _final_message docstring
    for why the earlier "message.completed"/single-content-field shape was
    fabricated and never matched opencode's actual output. Proves the fix
    joins every segment in the order it was observed, not just the last
    one -- the subtlest part of this fix to get wrong."""
    from runtime.runtime import OpencodeRuntime

    _use_tmp_session_root(monkeypatch, tmp_path)
    driver = _FakeDriver(
        [
            '{"type":"text","sessionID":"opencode-sess-2",'
            '"part":{"type":"text","text":"First, I found the bug.","time":{"end":1}}}',
            '{"type":"tool_use","sessionID":"opencode-sess-2",'
            '"part":{"type":"tool","state":{"status":"completed"}}}',
            '{"type":"text","sessionID":"opencode-sess-2",'
            '"part":{"type":"text","text":"Then I fixed it.","time":{"end":2}}}',
        ],
    )
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        result = await OpencodeRuntime().execute(
            db,
            agent=agent,
            task_text="fix the bug",
            tenant_id=tenant,
            run_id=run_id,
        )

    assert result.status == "done"
    assert result.output == "First, I found the bug.\nThen I fixed it."


# --------------------------------------------------------------------------
# Findings from the final whole-branch review. Every one of these is mirrored,
# in the same shape, in claude_code_runtime's and codex_runtime's own test
# files: the whole point of the review was that three near-identical plugins
# had drifted apart, so the tests that pin them have to stay identical too.
# --------------------------------------------------------------------------


async def test_the_session_directory_is_mounted_writable_so_a_resume_can_find_it(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--session <id>` reads the storage opencode writes under
    `Global.Path.data` -- `path.join(xdgData, "opencode")` then `storage/` per
    packages/core/src/global.ts and packages/opencode/src/storage/storage.ts,
    confirmed by running the real CLI inside this plugin's own image -- i.e.
    OUTSIDE /workspace, and teardown destroys the container filesystem at
    every park. So the flag alone proves nothing without a persistent,
    writable HOST directory behind that path.

    The config stays read-only, and -- unlike codex_runtime's config.toml --
    at a container path that is not nested inside the writable data directory
    at all, so no layering is needed to keep it out of the agent's reach."""
    from runtime.runtime import (
        _OPENCODE_CONFIG_PATH,
        _OPENCODE_DATA_DIR,
        OpencodeRuntime,
    )

    _use_tmp_session_root(monkeypatch, tmp_path)
    driver = _FakeDriver(['{"type":"text","sessionID":"opencode-sess-1","part":{"text":"Done."}}'])
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(
        app_session, tenant, {"cli_harness_sessions": {"opencode_runtime": "opencode-sess-1"}}
    )
    async with app_session(tenant) as db:
        db.add(agent)
        await OpencodeRuntime().execute(
            db, agent=agent, task_text="fix the bug", tenant_id=tenant, run_id=run_id
        )

    spec = driver.specs[0]
    assert "--session" in spec.command
    home = next(mnt for mnt in spec.mounts if mnt.container_path == _OPENCODE_DATA_DIR)
    assert home.readonly is False, "a read-only data dir cannot hold a resumable session"
    assert home.host_path.startswith(str(tmp_path))
    assert str(run_id) in home.host_path
    # Per-RUN, and NOT nested inside the agent-writable /workspace mount.
    workspace = next(mnt for mnt in spec.mounts if mnt.container_path == "/workspace")
    assert not home.host_path.startswith(workspace.host_path + "/")

    config = next(mnt for mnt in spec.mounts if mnt.container_path == _OPENCODE_CONFIG_PATH)
    assert config.readonly is True
    assert not config.container_path.startswith(_OPENCODE_DATA_DIR + "/")
    assert not config.host_path.startswith(home.host_path + "/")
    # XDG_DATA_HOME is pinned rather than inferred, so the mount above and the
    # CLI cannot disagree about where the data directory is.
    assert spec.env["XDG_DATA_HOME"] + "/opencode" == _OPENCODE_DATA_DIR


async def test_every_mount_source_exists_on_disk_before_provision(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Docker auto-creates a missing bind-mount source as root:root, which an
    unprivileged container then cannot write to -- silently defeating both
    /workspace and the resumable session."""
    from runtime.runtime import OpencodeRuntime

    _use_tmp_session_root(monkeypatch, tmp_path)
    seen: dict[str, bool] = {}

    class _OrderCheckingDriver(_FakeDriver):
        async def provision(self, spec: Any) -> SandboxHandle:
            seen["all_exist"] = all(
                # A local, cheap stat on a path this test just created.
                Path(mnt.host_path).exists()  # noqa: ASYNC240
                for mnt in spec.mounts
            )
            return await super().provision(spec)

    driver = _OrderCheckingDriver(['{"type":"text","sessionID":"s","part":{"text":"Done."}}'])
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        await OpencodeRuntime().execute(
            db, agent=agent, task_text="fix the bug", tenant_id=tenant, run_id=run_id
        )

    assert seen["all_exist"] is True


async def test_mounts_go_through_the_sandbox_allowlist(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """docker_driver.py documents sandbox.mounts.validate_mounts as a gate
    every caller must pass its bind-mount paths through."""
    import runtime.runtime as rt

    _use_tmp_session_root(monkeypatch, tmp_path)
    calls: list[str] = []
    real = rt.validate_mounts

    def _spy(mounts: Any, *, allowed_root: str) -> Any:
        calls.append(allowed_root)
        return real(mounts, allowed_root=allowed_root)

    monkeypatch.setattr(rt, "validate_mounts", _spy)
    driver = _FakeDriver(['{"type":"text","sessionID":"s","part":{"text":"Done."}}'])
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        await rt.OpencodeRuntime().execute(
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
    transition for the whole container run."""
    from runtime.runtime import OpencodeRuntime

    _use_tmp_session_root(monkeypatch, tmp_path)
    seen: dict[str, Any] = {}

    class _ObservingDriver(_FakeDriver):
        async def logs(self, handle: SandboxHandle) -> str:
            if "task_id" not in seen:
                async with app_session(tenant) as other:
                    row = await other.get(m.AgentRun, run_id)
                    seen["task_id"] = row.task_id if row is not None else None
            return await super().logs(handle)

    driver = _ObservingDriver(['{"type":"text","sessionID":"s","part":{"text":"Done."}}'])
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        await OpencodeRuntime().execute(
            db, agent=agent, task_text="fix the bug", tenant_id=tenant, run_id=run_id
        )

    assert seen["task_id"] is not None, "the row lock was still held while the container ran"


async def test_a_real_task_row_is_opened_and_reused_on_resume(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`run.task_id or uuid.uuid4()` -- what every RunResult here used to
    carry -- fabricates an id no row has ever had, so an approval raised
    against it can never be resolved back to the run."""
    from runtime.runtime import OpencodeRuntime

    _use_tmp_session_root(monkeypatch, tmp_path)
    driver = _FakeDriver(['{"type":"text","sessionID":"s","part":{"text":"Done."}}'])
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        result = await OpencodeRuntime().execute(
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
        again = await OpencodeRuntime().execute(
            db, agent=agent, task_text="carry on", tenant_id=tenant, run_id=run_id
        )
    assert again.task_id == result.task_id


async def test_an_operator_cancel_interrupts_the_poll_loop(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§7.2. nanoclaw_runtime probes cancel_check every poll iteration; this
    plugin accepted the parameter and never called it."""
    from runtime.runtime import OpencodeRuntime

    _use_tmp_session_root(monkeypatch, tmp_path)
    driver = _NeverFinishingDriver(['{"type":"tool_use","sessionID":"opencode-sess-4","part":{}}'])
    _install(monkeypatch, driver)

    async def _cancelled() -> bool:
        return True

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        started = time.monotonic()
        result = await OpencodeRuntime().execute(
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
    run can lose its `text` parts off the front between polls -- and
    done-vs-failed is decided entirely on having seen them. That is not the
    same event as "the task failed"."""
    from runtime.runtime import OpencodeRuntime

    _use_tmp_session_root(monkeypatch, tmp_path)
    driver = _FakeDriver(['{"type":"tool_use","sessionID":"s","part":{}}'], exit_code=0)
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        result = await OpencodeRuntime().execute(
            db, agent=agent, task_text="fix the bug", tenant_id=tenant, run_id=run_id
        )

    assert result.status == "failed"
    assert "no terminal event" in result.output


async def test_the_session_id_is_committed_once_not_every_poll(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`sessionID` rides on EVERY emitted line, so without a guard the id is
    re-derived and re-committed on every poll batch -- unlike
    claude_code_runtime (system/init) and codex_runtime (thread.started),
    which each see theirs once. A resume leg is the sharpest case: the id is
    already known before the first poll, so it should never be re-written at
    all."""
    from runtime.runtime import OpencodeRuntime

    _use_tmp_session_root(monkeypatch, tmp_path)
    driver = _NeverFinishingDriver(['{"type":"tool_use","sessionID":"opencode-sess-1","part":{}}'])
    _install(monkeypatch, driver)
    monkeypatch.setattr("runtime.runtime._RUN_TIMEOUT_S", 0.05)

    writes: list[Any] = []
    import runtime.runtime as rt

    real = rt.set_session_id

    async def _spy(db: Any, run: Any, plugin_name: str, session_id: str) -> Any:
        writes.append(session_id)
        return await real(db, run, plugin_name, session_id)

    monkeypatch.setattr(rt, "set_session_id", _spy)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(
        app_session, tenant, {"cli_harness_sessions": {"opencode_runtime": "opencode-sess-1"}}
    )
    async with app_session(tenant) as db:
        db.add(agent)
        await OpencodeRuntime().execute(
            db, agent=agent, task_text="fix the bug", tenant_id=tenant, run_id=run_id
        )

    assert writes == [], "the id was already known; nothing should have been re-committed"


async def test_the_toolchain_note_is_appended_to_the_task_prompt(
    app_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The brief's literal `_FakeDriver(["done"], exit_code=0)` does not match
    this plugin's real stdout-line shape: OpencodeRuntime parses each line as
    a JSON opencode event (see test_a_finished_run_returns_done above), not a
    bare string, so a plain "done" line would blow up in json.loads() before
    ever reaching the assertion this test actually needs. Using the same
    JSON "text" event shape as the other tests in this file instead."""
    from runtime.runtime import OpencodeRuntime

    _use_tmp_session_root(monkeypatch, tmp_path)
    driver = _FakeDriver(
        [
            '{"type":"text","sessionID":"opencode-sess-1",'
            '"part":{"type":"text","text":"done","time":{"end":123456}}}',
        ],
        exit_code=0,
    )
    _install(monkeypatch, driver)

    tenant = uuid.uuid4()
    agent, run_id = await _seed_agent(app_session, tenant, {})
    async with app_session(tenant) as db:
        db.add(agent)
        await OpencodeRuntime().execute(
            db, agent=agent, task_text="fix the bug", tenant_id=tenant, run_id=run_id,
        )

    spec = driver.specs[0]
    assert spec.command[-1].startswith("fix the bug\n\n")
    assert "Playwright" in spec.command[-1]
