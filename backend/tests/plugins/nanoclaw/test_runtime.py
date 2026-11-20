"""The adapter: oc8 as nanoclaw's host, for one run.

Fake sandbox driver throughout -- what is under test is the ORCHESTRATION (what
the container is given, what is polled, when the run ends), not Docker.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

# tests/plugins/nanoclaw/test_runtime.py -> repo root -> plugins/nanoclaw_runtime.
# See test_messages.py for why this is per-file rather than pytest's `pythonpath`.
# This file is MIXED -- module-level plugin imports just below AND ~35 function-local
# ones further down -- so it needs BOTH forms: the module-top prelude for collection
# time, and the `_plugin_path` autouse fixture for execution time.
PLUGIN_ROOT = Path(__file__).resolve().parents[4] / "capas" / "nanoclaw_runtime"


def _evict() -> None:
    """Drop every cached `runtime`/`runtime.*` module from sys.modules.

    `runtime` is the package name EVERY `runtime_adapter` plugin now ships --
    nanoclaw_runtime, claude_code_runtime, codex_runtime and opencode_runtime,
    four plugins behind one top-level module name (design §2) -- and
    sys.modules is keyed by NAME, not by path. Called SYMMETRICALLY, before
    the import AND after it: before, so a sibling runtime's cached copy cannot
    answer ours; after, so nothing generic is left cached for anyone else.
    The trailing half is the load-bearing one -- `loader.import_entry_point`
    (Task 6's collision fix) only evicts modules IT ITSELF introduced, so a
    `runtime` left cached here makes a later `find_plugin`/`load_plugin` for
    one of the other three silently hand back THIS plugin's `register`.
    """
    for _stale in [n for n in sys.modules if n == "runtime" or n.startswith("runtime.")]:
        del sys.modules[_stale]


_evict()
sys.path.insert(0, str(PLUGIN_ROOT))

from runtime.runtime import _is_gateway_error  # noqa: E402
from runtime.session import MCP_TOOL_PREFIX, group_dir, session_dir  # noqa: E402
from sqlalchemy import text as sql_text  # noqa: E402

from oc8 import models as m  # noqa: E402
from oc8.auth import get_identity_provider  # noqa: E402
from oc8.runtime.states import RunState  # noqa: E402
from oc8.sandbox.types import SandboxHandle  # noqa: E402
from tests.conftest import AppSessionFactory  # noqa: E402

# Symmetric with the insert above: the names this module-level import needed
# are already bound here, so leaving `runtime` cached would only serve to hand
# THIS plugin's module to whichever sibling runtime's test file collects next.
_evict()
sys.path.remove(str(PLUGIN_ROOT))

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _plugin_path() -> Iterator[None]:
    """Make THIS plugin's package the one that resolves, for the duration of each test.

    `runtime`/`connector`/`channel`/`provider`/`mcp_bridge` are generic names every
    plugin reuses (design §2), and sys.modules is keyed by NAME, not by path -- so a
    sibling plugin's tests would otherwise hand us ITS code. This is a FUNCTION-scoped
    autouse fixture, deliberately, not a module-top prelude: most of this repo's plugin
    tests import their plugin INSIDE the test body, which resolves at execution time,
    long after collection-time module-top code has run. A module-top prelude silently
    does nothing for those files. The already-imported module objects other test files
    hold stay valid across the eviction.

    This plugin's package IS the generic `runtime/` now (the folder convention for
    every `runtime_adapter` plugin), which is exactly why the eviction is mandatory
    rather than defensive: `import runtime` is ambiguous across four plugin roots and
    only this fixture makes it mean THIS one.
    """
    _evict()
    sys.path.insert(0, str(PLUGIN_ROOT))
    yield
    sys.path.remove(str(PLUGIN_ROOT))
    _evict()


# Mirrors only what oc8 touches, including the two routing tables write_routing
# fills -- the real schema comes from the pinned image (test_schema_contract.py).
_INBOUND = """
CREATE TABLE messages_in (
  id TEXT PRIMARY KEY, seq INTEGER UNIQUE, kind TEXT NOT NULL, timestamp TEXT NOT NULL,
  status TEXT DEFAULT 'pending', trigger INTEGER NOT NULL DEFAULT 1,
  -- Routing columns are NOT decoration: the harness resolves an inbound
  -- message's from="..." through them, and a NULL pair made the model guess a
  -- destination name, guess wrong, and have its answer dropped while the batch
  -- was still acked completed (found live, 2026-07-27).
  channel_type TEXT, platform_id TEXT, content TEXT NOT NULL);
CREATE TABLE destinations (
  name TEXT PRIMARY KEY, display_name TEXT, type TEXT NOT NULL, channel_type TEXT,
  platform_id TEXT, agent_group_id TEXT);
CREATE TABLE session_routing (
  id INTEGER PRIMARY KEY CHECK (id = 1), channel_type TEXT, platform_id TEXT, thread_id TEXT);
"""
_OUTBOUND = """
CREATE TABLE messages_out (
  id TEXT PRIMARY KEY, seq INTEGER UNIQUE, timestamp TEXT NOT NULL, kind TEXT NOT NULL,
  content TEXT NOT NULL);
CREATE TABLE processing_ack (
  message_id TEXT PRIMARY KEY, status TEXT NOT NULL, status_changed TEXT NOT NULL);
"""
_AGENT_CONTAINER = "agentcontainer"


class FakeDriver:
    """Provisions nothing. On `provision` of the agent container it plays the
    container's part: it answers the task and acks it, which is what the adapter
    is waiting for. `exit_code` is what the container's process is deemed to have
    returned, so a test can also play a container that dies."""

    def __init__(self, session_root: str, *, reply: str | None = "Erledigt.") -> None:
        self.session_root = session_root
        self.specs: list[Any] = []
        self.reply = reply
        self.exit_code = 0
        self.ack = "completed"
        self.torn_down = 0
        self.logs_called = 0

    async def provision(self, spec: Any) -> SandboxHandle:
        self.specs.append(spec)
        session = [mnt.host_path for mnt in spec.mounts if mnt.container_path == "/workspace"]
        if session:  # the agent container, not the provisioner
            self._play_container(session[0])
            return SandboxHandle(container_id=_AGENT_CONTAINER, image=spec.image)
        # the provisioner: create the DBs
        target = spec.mounts[0].host_path
        sqlite3.connect(f"{target}/inbound.db").executescript(_INBOUND)
        sqlite3.connect(f"{target}/outbound.db").executescript(_OUTBOUND)
        return SandboxHandle(container_id="provisionercontainer", image=spec.image)

    def _play_container(self, session: str) -> None:
        with sqlite3.connect(f"{session}/inbound.db") as conn:
            row = conn.execute("SELECT id FROM messages_in ORDER BY seq DESC LIMIT 1").fetchone()
        with sqlite3.connect(f"{session}/outbound.db") as conn:
            if self.reply is not None:
                # A resume leg reuses the same outbound.db as the leg before it
                # (that is the whole point), so seq cannot be a literal 1 -- a
                # second leg would collide with the first leg's own row.
                (next_seq,) = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages_out"
                ).fetchone()
                conn.execute(
                    "INSERT INTO messages_out (id, seq, timestamp, kind, content)"
                    " VALUES (?, ?, 't', 'chat', ?)",
                    (str(uuid.uuid4()), next_seq, json.dumps({"text": self.reply})),
                )
            if self.ack:
                conn.execute(
                    "INSERT INTO processing_ack (message_id, status, status_changed)"
                    " VALUES (?, ?, 't')",
                    (row[0], self.ack),
                )
            conn.commit()
        open(f"{session}/.heartbeat", "w").close()

    async def wait(self, handle: Any, timeout_s: float = 0) -> int:
        # Only the agent container's process is scripted -- the provisioner must
        # always succeed, or the session DBs never exist in the first place.
        return self.exit_code if handle.container_id == _AGENT_CONTAINER else 0

    async def logs(self, handle: Any) -> str:
        self.logs_called += 1
        return ""

    async def teardown(self, handle: Any) -> None:
        self.torn_down += 1


class DeadDriver(FakeDriver):
    """A container that starts and dies: no DB writes, no heartbeat, and its
    process is gone. Nothing will ever arrive."""

    def _play_container(self, session: str) -> None:
        return None


class SilentDriver(FakeDriver):
    """A container that is ALIVE but never says anything: no heartbeat, no rows,
    and a process that never exits. Neither watchdog can see it without a bound
    of its own."""

    def _play_container(self, session: str) -> None:
        return None

    async def wait(self, handle: Any, timeout_s: float = 0) -> int:
        if handle.container_id != _AGENT_CONTAINER:
            return 0
        await asyncio.sleep(timeout_s)
        return 0


class StaleHeartbeatDriver(FakeDriver):
    """A container that touches .heartbeat exactly once -- scheduled to land
    after `_drive()` has started its clock, the way a held tool-approval leaves
    a real one -- then goes silent while its process keeps running. Only the
    stale-heartbeat watchdog can end this run."""

    def _play_container(self, session: str) -> None:
        self.script = asyncio.ensure_future(self._touch_once(session))

    async def _touch_once(self, session: str) -> None:
        await asyncio.sleep(0.02)
        _touch_heartbeat(session)

    async def wait(self, handle: Any, timeout_s: float = 0) -> int:
        if handle.container_id != _AGENT_CONTAINER:
            return 0
        await asyncio.sleep(timeout_s)
        return 0


class ParkingDriver(FakeDriver):
    """A container that says nothing, while ANOTHER transaction (the gateway)
    marks the run as held. That second transaction is the point: the adapter has
    to re-read its run row to see it."""

    park_run_id: uuid.UUID | None = None
    session_factory: Any = None
    tenant: uuid.UUID | None = None

    def _play_container(self, session: str) -> None:
        self.script = asyncio.ensure_future(self._gateway_parks())

    async def _gateway_parks(self) -> None:
        await asyncio.sleep(0.05)
        async with self.session_factory(self.tenant) as db:
            run = await db.get(m.AgentRun, self.park_run_id)
            run.context = {
                **run.context,
                "isolated_result": {
                    "status": "waiting_for_approval",
                    "output": "value €7500 meets threshold €3000",
                },
            }

    async def wait(self, handle: Any, timeout_s: float = 0) -> int:
        if handle.container_id != _AGENT_CONTAINER:
            return 0
        await asyncio.sleep(timeout_s)
        return 0


class ScriptedDriver(FakeDriver):
    """A container that answers over TIME, the way a real one does: an interim
    message on one poll, the answer and its ack on a later one. This is the only
    driver that makes the adapter sweep more than once."""

    def __init__(self, session_root: str, *, step_s: float) -> None:
        super().__init__(session_root)
        self.step_s = step_s
        self.script: asyncio.Task[None] | None = None

    def _play_container(self, session: str) -> None:
        self.script = asyncio.ensure_future(self._answer_over_time(session))

    async def _answer_over_time(self, session: str) -> None:
        await asyncio.sleep(self.step_s)
        _say(session, seq=1, text="Ich schaue nach.")
        await asyncio.sleep(self.step_s * 2)
        _say(session, seq=3, text="Erledigt.", ack=_task_message_id(session))

    async def wait(self, handle: Any, timeout_s: float = 0) -> int:
        if handle.container_id != _AGENT_CONTAINER:
            return 0
        await asyncio.sleep(timeout_s)
        return 0

    async def teardown(self, handle: Any) -> None:
        if self.script is not None:
            self.script.cancel()
        await super().teardown(handle)


def _touch_heartbeat(session: str) -> None:
    # Sync on purpose (see _say below): kept out of the calling async method's
    # body so ASYNC230 doesn't flag a blocking open() inside an async def.
    open(f"{session}/.heartbeat", "w").close()


def _say(session: str, *, seq: int, text: str, ack: str | None = None) -> None:
    """The container's side of the protocol: an outbound row, odd seq (the host
    writes even ones), plus the ack that ends the turn."""
    with sqlite3.connect(f"{session}/outbound.db") as conn:
        conn.execute(
            "INSERT INTO messages_out (id, seq, timestamp, kind, content) VALUES (?, ?, 't', ?, ?)",
            (str(uuid.uuid4()), seq, "chat", json.dumps({"text": text})),
        )
        if ack is not None:
            conn.execute(
                "INSERT INTO processing_ack (message_id, status, status_changed)"
                " VALUES (?, 'completed', 't')",
                (ack,),
            )
        conn.commit()
    open(f"{session}/.heartbeat", "w").close()


def _task_message_id(session: str) -> str:
    """The FIRST inbound row: the task. A later steering message must not be what
    the adapter's ack is looking for."""
    with sqlite3.connect(f"{session}/inbound.db") as conn:
        return str(conn.execute("SELECT id FROM messages_in ORDER BY seq").fetchone()[0])


def _inbound(session: str) -> list[tuple[int, str]]:
    with sqlite3.connect(f"{session}/inbound.db") as conn:
        return [
            (int(seq), str(json.loads(content)["text"]))
            for seq, content in conn.execute("SELECT seq, content FROM messages_in ORDER BY seq")
        ]


def _read(*parts: Any) -> str:
    # Sync helper on purpose: a one-off assertion read, once the run is over.
    return Path(*[str(p) for p in parts]).read_text(encoding="utf-8")


def _mode(*parts: Any) -> str:
    return oct(os.stat(os.path.join(*[str(p) for p in parts])).st_mode & 0o777)


def _install(monkeypatch: pytest.MonkeyPatch, driver: FakeDriver, root: str) -> None:
    monkeypatch.setattr("runtime.runtime.get_sandbox_driver", lambda: driver)
    monkeypatch.setattr("runtime.runtime.SESSION_ROOT_OVERRIDE", root, raising=False)


async def _agent_and_run(db: Any, tenant: uuid.UUID) -> tuple[m.Agent, uuid.UUID]:
    dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name="Nora",
        status="running",
        mission="Du verkaufst Gartenhäuser.",
        narrowing={},
        definition={},
        presentation={},
    )
    db.add(agent)
    await db.flush()
    return agent, await _new_run(db, tenant, agent)


async def _assign_skill(db: Any, tenant: uuid.UUID, agent: m.Agent) -> None:
    skill = m.Skill(
        tenant_id=tenant,
        name="Invoice Review",
        description="Validates invoices against POs.",
        author="oc8 core",
    )
    db.add(skill)
    await db.flush()
    version = m.SkillVersion(
        tenant_id=tenant,
        skill_id=skill.id,
        semver="1.0.0",
        artifact_hash=b"\x00" * 32,
        definition={
            "oc8_skill": 1,
            "id": "sk-invoice-check",
            "version": "1.0.0",
            "instruction": "Match invoices against purchase orders.",
            "requires": {"tools": [], "kbs": []},
            "guardrails": [],
        },
    )
    db.add(version)
    await db.flush()
    skill.current_version_id = version.id
    db.add(
        m.SkillAssignment(
            tenant_id=tenant, agent_id=agent.id, skill_version_id=version.id, enabled=True
        )
    )
    await db.flush()


async def _new_run(db: Any, tenant: uuid.UUID, agent: m.Agent) -> uuid.UUID:
    run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state=RunState.RUNNING.value, context={})
    db.add(run)
    await db.flush()
    run_id: uuid.UUID = run.id
    return run_id


async def test_a_run_completes_from_the_harnesss_answer(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from runtime.runtime import NanoclawRuntime

    driver = FakeDriver(str(tmp_path))
    _install(monkeypatch, driver, str(tmp_path))

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        result = await NanoclawRuntime().execute(
            db, agent=agent, task_text="Erstelle ein Angebot", tenant_id=tenant, run_id=run_id
        )

    assert result.status == "done"
    assert result.output == "Erledigt."
    assert driver.torn_down == 2, "both the provisioner and the agent container must be removed"

    async with app_session(tenant) as db:
        task = await db.get(m.Task, result.task_id)
        assert task is not None and task.state == "done"


async def test_the_container_gets_the_gateways_and_nothing_else(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The security claim of the whole slice: no provider key, no tool
    credential, no DB URL ever enters the container."""
    from runtime.runtime import NanoclawRuntime

    driver = FakeDriver(str(tmp_path))
    _install(monkeypatch, driver, str(tmp_path))

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, first_run = await _agent_and_run(db, tenant)
        await NanoclawRuntime().execute(
            db, agent=agent, task_text="hallo", tenant_id=tenant, run_id=first_run
        )
        second_run = await _new_run(db, tenant, agent)
        await NanoclawRuntime().execute(
            db, agent=agent, task_text="nochmal", tenant_id=tenant, run_id=second_run
        )

    from oc8.sandbox.reaper import TENANT_LABEL

    # Every container the runtime starts, the session provisioner included.
    assert driver.specs
    assert all(s.labels[TENANT_LABEL] == str(tenant) for s in driver.specs)
    first, second = [
        s for s in driver.specs if any(mnt.container_path == "/workspace" for mnt in s.mounts)
    ]
    assert set(first.env) == {
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "OC8_MCP_URL",
        "OC8_TOKEN",
        "TZ",
        "HOME",
    }
    assert first.env["ANTHROPIC_AUTH_TOKEN"] == first.env["OC8_TOKEN"]
    assert "/llm" in first.env["ANTHROPIC_BASE_URL"]
    assert "/mcp" in first.env["OC8_MCP_URL"]

    # The other half of the claim: the one credential in there reaches exactly
    # ONE run. A token that outlived its run would let a finished container keep
    # spending the tenant's budget and acting on a run nobody is watching.
    provider = get_identity_provider()
    principal = provider.verify(first.env["OC8_TOKEN"])
    assert principal.kind == "agent"
    assert principal.tenant_id == tenant
    assert principal.subject == f"agent:{agent.id}"
    assert principal.scopes == [f"run:{first_run}"]
    assert provider.verify(second.env["OC8_TOKEN"]).scopes == [f"run:{second_run}"]
    assert first.env["OC8_TOKEN"] != second.env["OC8_TOKEN"]


async def test_an_operator_message_reaches_the_harness_as_a_row(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from runtime.runtime import NanoclawRuntime

    driver = FakeDriver(str(tmp_path))
    _install(monkeypatch, driver, str(tmp_path))

    delivered: list[str] = []

    async def inbox() -> list[str]:
        if not delivered:
            delivered.append("mach es bitte kleiner")
            return ["mach es bitte kleiner"]
        return []

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        await NanoclawRuntime().execute(
            db,
            agent=agent,
            task_text="Angebot",
            tenant_id=tenant,
            run_id=run_id,
            inbox_check=inbox,
        )

    session = session_dir(str(tmp_path), agent.id, run_id)
    with sqlite3.connect(f"{session}/inbound.db") as conn:
        texts = [
            json.loads(r[0])["text"]
            for r in conn.execute("SELECT content FROM messages_in ORDER BY seq")
        ]
    assert "mach es bitte kleiner" in texts


async def test_claude_md_holds_only_what_the_agent_itself_implies(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """CLAUDE.md is written fresh into each run's OWN per-run group folder (see
    `nanoclaw_runtime/runtime/session.py`), so nothing run-scoped can leak from one run's
    file into another's through the filesystem. It still must not depend on
    which run wrote it, though: a task, or knowledge admitted for one run's
    model locality, would otherwise make an agent's standing instructions drift
    with whichever run happened to write them last. The invariant this asserts
    is that two different runs of the same agent produce IDENTICAL bytes."""
    from runtime.runtime import NanoclawRuntime

    driver = FakeDriver(str(tmp_path))
    _install(monkeypatch, driver, str(tmp_path))
    # Sentinels on the two RETRIEVAL paths of the shared preamble. Retrieved
    # knowledge is admitted against the model locality at the moment of retrieval,
    # so it belongs to a run, not to an agent -- this runtime must not reach for
    # it at all while there is no per-run channel to carry it.
    retrieved: list[str] = []

    async def _kb(*args: Any, **kwargs: Any) -> tuple[str, bool]:
        retrieved.append("kb")
        return "KB-CONTEXT-SENTINEL", True

    async def _memory(*args: Any, **kwargs: Any) -> str:
        retrieved.append("memory")
        return "MEMORY-CONTEXT-SENTINEL"

    monkeypatch.setattr("oc8.agent.preamble.retrieve_kb_context", _kb)
    monkeypatch.setattr("oc8.agent.preamble.retrieve_context", _memory)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, first_run = await _agent_and_run(db, tenant)
        await _assign_skill(db, tenant, agent)
        await NanoclawRuntime().execute(
            db, agent=agent, task_text="Erstelle ein Angebot", tenant_id=tenant, run_id=first_run
        )
        after_first = _read(group_dir(str(tmp_path), agent.id, first_run), "CLAUDE.md")
        second_run = await _new_run(db, tenant, agent)
        await NanoclawRuntime().execute(
            db,
            agent=agent,
            task_text="Storniere die Bestellung",
            tenant_id=tenant,
            run_id=second_run,
        )

    claude_md = _read(group_dir(str(tmp_path), agent.id, second_run), "CLAUDE.md")
    assert claude_md == after_first, "CLAUDE.md content must not depend on which run wrote it"
    assert "You are Nora" in claude_md
    assert "Du verkaufst Gartenhäuser." in claude_md
    # Skills are agent-scoped, and Task 8's `skills` capability claim rests on
    # them arriving exactly here.
    assert "Invoice Review" in claude_md
    assert "Erstelle ein Angebot" not in claude_md
    assert "Storniere die Bestellung" not in claude_md
    assert retrieved == [], "this runtime must not retrieve knowledge into CLAUDE.md"
    assert "SENTINEL" not in claude_md
    assert _mode(group_dir(str(tmp_path), agent.id, second_run), "CLAUDE.md") == "0o600"

    # The task reaches the harness the only way it may: as an inbound message.
    # startswith, not equality: every wake message now carries the delivery
    # reminder appended after the instruction (see _DELIVERY_REMINDER). What this
    # asserts is which TASK the harness was woken with.
    inbound = _inbound(session_dir(str(tmp_path), agent.id, first_run))
    assert [seq for seq, _text in inbound] == [2]
    assert inbound[0][1].startswith("Erstelle ein Angebot")


async def test_a_dead_container_fails_the_run_instead_of_waiting_out_the_deadline(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A container that dies on startup (bad image, bad command) never writes a
    heartbeat, so the stale-heartbeat watchdog cannot see it: without watching the
    process itself the run would sit there for agent_max_steps minutes."""
    from runtime.runtime import NanoclawRuntime

    driver = DeadDriver(str(tmp_path))
    driver.exit_code = 127
    _install(monkeypatch, driver, str(tmp_path))

    tenant = uuid.uuid4()
    started = time.monotonic()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        result = await NanoclawRuntime().execute(
            db, agent=agent, task_text="Erstelle ein Angebot", tenant_id=tenant, run_id=run_id
        )

    assert result.status == "failed"
    assert time.monotonic() - started < 30, "the run must not wait out its whole deadline"

    async with app_session(tenant) as db:
        task = await db.get(m.Task, result.task_id)
        assert task is not None and task.state == "failed"


async def test_completed_with_nothing_said_is_a_failure_not_an_empty_success(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Task 0's Q1 signature: the harness acks a turn it could not deliver.
    Reporting `done` with an empty output would hand an operator a silent lie."""
    from runtime.runtime import NanoclawRuntime

    driver = FakeDriver(str(tmp_path), reply=None)
    _install(monkeypatch, driver, str(tmp_path))

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        result = await NanoclawRuntime().execute(
            db, agent=agent, task_text="Erstelle ein Angebot", tenant_id=tenant, run_id=run_id
        )

    assert result.status == "failed"
    assert result.output


async def test_a_cancelled_run_is_interrupted_and_its_task_lands_on_a_legal_state(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """`interrupted` is a RUN state, not a task state -- the task table's check
    constraint rejects it, so writing the run's verdict straight onto the task
    would abort the transaction and strand the run."""
    from runtime.runtime import NanoclawRuntime

    driver = FakeDriver(str(tmp_path))
    _install(monkeypatch, driver, str(tmp_path))

    async def cancelled() -> bool:
        return True

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        result = await NanoclawRuntime().execute(
            db,
            agent=agent,
            task_text="Erstelle ein Angebot",
            tenant_id=tenant,
            run_id=run_id,
            cancel_check=cancelled,
        )

    assert result.status == "interrupted"
    async with app_session(tenant) as db:
        task = await db.get(m.Task, result.task_id)
        assert task is not None and task.state == "failed"


async def test_an_unrecordable_message_does_not_kill_an_otherwise_healthy_run(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The activity feed is a view of the run, not the run. A failed write there
    must not throw away work the container really did.

    The failure is a real DB error, not a raise before any statement: a failed
    statement POISONS the surrounding transaction, so every later statement in
    the run (the terminal task state, on the way out) fails too. Only a rollback
    clears that, and a session-wide rollback would expire the identity map -- so
    a plain try/except, or an except that rolls the whole session back, both go
    red here while the savepoint passes.
    """
    from runtime.runtime import NanoclawRuntime

    async def poisons_the_transaction(db: Any, **kwargs: Any) -> None:
        await db.execute(sql_text("SELECT 1 / 0"))

    driver = FakeDriver(str(tmp_path))
    _install(monkeypatch, driver, str(tmp_path))
    monkeypatch.setattr("runtime.runtime.record_activity", poisons_the_transaction)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        result = await NanoclawRuntime().execute(
            db, agent=agent, task_text="Erstelle ein Angebot", tenant_id=tenant, run_id=run_id
        )
        # The run's own transaction is still usable afterwards.
        assert (await db.get(m.Agent, agent.id)) is not None

    assert result.status == "done"
    assert result.output == "Erledigt."

    async with app_session(tenant) as db:
        task = await db.get(m.Task, result.task_id)
        assert task is not None and task.state == "done", "the terminal write must still land"
        events = (
            await db.execute(
                m.ActivityEvent.__table__.select().where(m.ActivityEvent.agent_id == agent.id)
            )
        ).fetchall()
        assert events == [], "the poisoned write must leave nothing behind"


async def test_a_container_that_never_comes_up_is_bounded_by_its_own_grace_window(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Alive but silent: no heartbeat ever appears, so the stale watchdog has no
    timestamp to age, and the process never exits, so the exit watch never fires.
    Without a bound of its own this burns the whole deadline and then reports a
    generic timeout -- which sends an operator looking in the wrong place."""
    from runtime.runtime import NanoclawRuntime

    driver = SilentDriver(str(tmp_path))
    _install(monkeypatch, driver, str(tmp_path))
    monkeypatch.setattr("runtime.runtime._POLL_SECONDS", 0.05)
    monkeypatch.setattr("runtime.runtime._STARTUP_GRACE_S", 0.2)

    tenant = uuid.uuid4()
    started = time.monotonic()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        result = await NanoclawRuntime().execute(
            db, agent=agent, task_text="Erstelle ein Angebot", tenant_id=tenant, run_id=run_id
        )

    assert result.status == "failed"
    assert "never came up" in result.output
    assert time.monotonic() - started < 30


async def test_a_stale_heartbeat_failure_still_reads_the_containers_log(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Task 9c, live: a run stuck waiting on a held tool-approval never touched
    .heartbeat again and was failed by the stale-heartbeat watchdog -- but that
    watchdog, and its two siblings (never-came-up, deadline-exceeded), `return`
    straight out of `_drive()`'s loop, bypassing the `_log_container` call the
    `_sweep()`-verdict branch already had. The container's log is the only
    account of why it stopped; without this, the exact case that just left a
    live run undiagnosable would keep doing so.
    """
    from runtime.runtime import NanoclawRuntime

    driver = StaleHeartbeatDriver(str(tmp_path))
    _install(monkeypatch, driver, str(tmp_path))
    monkeypatch.setattr("runtime.runtime._POLL_SECONDS", 0.05)
    monkeypatch.setattr("runtime.runtime._HEARTBEAT_STALE_S", 0.15)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        result = await NanoclawRuntime().execute(
            db, agent=agent, task_text="Erstelle ein Angebot", tenant_id=tenant, run_id=run_id
        )

    assert result.status == "failed"
    assert "stopped responding" in result.output
    assert driver.logs_called >= 1, "the container's own log is the only account of why"


async def test_an_answer_that_arrives_over_several_polls_is_streamed_exactly_once(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The shape a real run has: the harness talks across several polls, and the
    operator steers it in between. Each outbound row must reach the activity feed
    exactly once (a `seen_seq` that failed to advance would replay every earlier
    message on every poll), and the run must end on the ack of the TASK message,
    not of the steering message that arrived after it."""
    from runtime.runtime import NanoclawRuntime

    driver = ScriptedDriver(str(tmp_path), step_s=0.15)
    _install(monkeypatch, driver, str(tmp_path))
    monkeypatch.setattr("runtime.runtime._POLL_SECONDS", 0.05)

    polls = 0

    async def inbox() -> list[str]:
        # Nothing on the first sweep; a steering message once the run is going.
        nonlocal polls
        polls += 1
        return ["mach es kleiner"] if polls == 2 else []

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        result = await NanoclawRuntime().execute(
            db,
            agent=agent,
            task_text="Angebot",
            tenant_id=tenant,
            run_id=run_id,
            inbox_check=inbox,
        )

    assert result.status == "done"
    assert result.output == "Erledigt."

    # Even seqs, in order: the host's half of the parity rule holds across polls.
    # The texts are compared by prefix -- every wake message carries the delivery
    # reminder appended after the instruction (see _DELIVERY_REMINDER) -- since
    # what this asserts is the SEQUENCE, not the wording.
    inbound = _inbound(session_dir(str(tmp_path), agent.id, run_id))
    assert [seq for seq, _text in inbound] == [2, 4]
    assert inbound[0][1].startswith("Angebot")
    assert inbound[1][1].startswith("mach es kleiner")

    async with app_session(tenant) as db:
        events = (
            await db.execute(
                m.ActivityEvent.__table__.select().where(m.ActivityEvent.agent_id == agent.id)
            )
        ).fetchall()
    assert sorted(e.message for e in events) == ["Erledigt.", "Ich schaue nach."]


async def test_the_container_is_torn_down_even_when_driving_blows_up(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A leaked container keeps a run-scoped token alive and holds its session
    folder open, so teardown belongs in a finally, not on the happy path."""
    from runtime.runtime import NanoclawRuntime

    driver = FakeDriver(str(tmp_path))
    _install(monkeypatch, driver, str(tmp_path))

    async def exploding_inbox() -> list[str]:
        raise RuntimeError("the operator inbox is unreachable")

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        with pytest.raises(RuntimeError, match="unreachable"):
            await NanoclawRuntime().execute(
                db,
                agent=agent,
                task_text="Angebot",
                tenant_id=tenant,
                run_id=run_id,
                inbox_check=exploding_inbox,
            )

    assert driver.torn_down == 2


async def test_the_agent_is_told_how_this_harness_delivers_an_answer(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """oc8's shared preamble ends with "reply with a short plain-text summary".
    Correct in-process, fatal here: this harness delivers ONLY text wrapped in a
    `<message to="...">` block and logs anything else as scratchpad -- then acks
    the batch completed. Observed live 2026-07-27: the model read Odoo, answered
    "Es gibt aktuell 17 Leads" in plain text, and the run finished with no output.
    So the standing instructions must carry the delivery rule, it must come after
    the contradicting one, and it must name the destination write_routing creates.
    """
    from runtime.runtime import NanoclawRuntime
    from runtime.session_db import ROUTE_NAME

    driver = FakeDriver(str(tmp_path))
    monkeypatch.setattr("runtime.runtime.get_sandbox_driver", lambda: driver)
    monkeypatch.setattr("runtime.runtime.SESSION_ROOT_OVERRIDE", str(tmp_path))

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        await NanoclawRuntime().execute(
            db, agent=agent, task_text="Wie viele Leads?", tenant_id=tenant, run_id=run_id
        )

    claude_md = _read(group_dir(str(tmp_path), agent.id, run_id), "CLAUDE.md")
    assert f'<message to="{ROUTE_NAME}">' in claude_md
    summary_pos = claude_md.find("Zusammenfassung")
    if summary_pos != -1:
        assert claude_md.find("<message to=") > summary_pos, (
            "the delivery rule must come after the plain-text instruction it overrides"
        )


async def test_a_gateway_error_delivered_as_an_answer_fails_the_run(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The harness has a last-resort delivery: a turn that ends in an error with
    no <message> envelope is sent as if it were the answer, and still acked
    `completed`. Observed live 2026-07-27: ten retries against a provider 400,
    then oc8's own 502 text handed to the operator as the agent's reply, run
    marked done. We can recognise it because the text is OUR error format."""
    from runtime.runtime import NanoclawRuntime

    driver = FakeDriver(
        str(tmp_path),
        reply="502 from http://backend:8099/llm/v1/messages: upstream rejected the request",
    )
    monkeypatch.setattr("runtime.runtime.get_sandbox_driver", lambda: driver)
    monkeypatch.setattr("runtime.runtime.SESSION_ROOT_OVERRIDE", str(tmp_path))

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        result = await NanoclawRuntime().execute(
            db, agent=agent, task_text="Wie viele Leads?", tenant_id=tenant, run_id=run_id
        )

    assert result.status == "failed", "an upstream error is not an answer"
    assert "502" in result.output, "and the operator still gets to see what happened"


async def test_a_real_answer_that_merely_mentions_an_error_still_succeeds(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The guard matches shapes oc8 itself emits, not the word 'error' -- a model
    is allowed to report that something went wrong in Odoo."""
    from runtime.runtime import NanoclawRuntime

    driver = FakeDriver(
        str(tmp_path), reply="Der Lead konnte nicht angelegt werden: Fehler im CRM."
    )
    monkeypatch.setattr("runtime.runtime.get_sandbox_driver", lambda: driver)
    monkeypatch.setattr("runtime.runtime.SESSION_ROOT_OVERRIDE", str(tmp_path))

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        result = await NanoclawRuntime().execute(
            db, agent=agent, task_text="Lege einen Lead an", tenant_id=tenant, run_id=run_id
        )

    assert result.status == "done"


async def test_a_held_tool_call_parks_the_run_instead_of_starving_it(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gateway holds an over-threshold call and says so in run.context. Until
    the adapter reads that, the run stays `running`, the container falls silent
    waiting for a human, and the stale-heartbeat watchdog kills it -- observed
    live 2026-07-27: an operator who approved 16 minutes later got `resumed:
    false` and nothing happened. Parking is what puts the decision back in human
    time."""
    from runtime.runtime import NanoclawRuntime

    driver = ParkingDriver(str(tmp_path))
    monkeypatch.setattr("runtime.runtime.get_sandbox_driver", lambda: driver)
    monkeypatch.setattr("runtime.runtime.SESSION_ROOT_OVERRIDE", str(tmp_path))
    # Bounds: before the fix, nothing reads the marker, so the run would otherwise
    # burn the whole startup grace before reporting `failed`. Short-circuited here
    # so the RED run fails fast instead of taking five real minutes.
    monkeypatch.setattr("runtime.runtime._POLL_SECONDS", 0.02)
    monkeypatch.setattr("runtime.runtime._STARTUP_GRACE_S", 2.0)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        driver.park_run_id = run_id
        driver.session_factory = app_session
        driver.tenant = tenant
        result = await NanoclawRuntime().execute(
            db,
            agent=agent,
            task_text="Lege ein Angebot über 7500 an",
            tenant_id=tenant,
            run_id=run_id,
        )

    assert result.status == "waiting_for_approval"
    assert "7500" in result.output or "Freigabe" in result.output or "approval" in result.output
    assert driver.torn_down >= 1, "a parked run does not keep a container waiting for a human"


async def test_a_parked_runs_task_lands_on_a_legal_waiting_state(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`waiting_for_approval` is a legal task state (ck_task_state). Mapping it to
    `failed` -- as the current _TASK_STATE would -- tells the office view the work
    is over when a human is being waited for."""
    from runtime.runtime import NanoclawRuntime

    driver = ParkingDriver(str(tmp_path))
    monkeypatch.setattr("runtime.runtime.get_sandbox_driver", lambda: driver)
    monkeypatch.setattr("runtime.runtime.SESSION_ROOT_OVERRIDE", str(tmp_path))
    monkeypatch.setattr("runtime.runtime._POLL_SECONDS", 0.02)
    monkeypatch.setattr("runtime.runtime._STARTUP_GRACE_S", 2.0)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        driver.park_run_id = run_id
        driver.session_factory = app_session
        driver.tenant = tenant
        result = await NanoclawRuntime().execute(
            db,
            agent=agent,
            task_text="Lege ein Angebot über 7500 an",
            tenant_id=tenant,
            run_id=run_id,
        )

    async with app_session(tenant) as db:
        task = await db.get(m.Task, result.task_id)
        assert task is not None and task.state == "waiting_for_approval"


async def test_a_resume_leg_does_not_replay_what_the_first_leg_already_streamed(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """seen_seq starts at 0 on a fresh leg, so every message the first leg already
    put on the activity feed would be pushed again -- the operator would watch the
    agent repeat itself after every approval."""
    from runtime.runtime import NanoclawRuntime

    recorded: list[str] = []

    async def fake_record(
        db: Any,
        *,
        tenant_id: Any,
        agent_id: Any,
        status: str,
        message: str,
        detail: str | None = None,
    ) -> Any:
        recorded.append(message)
        return None

    monkeypatch.setattr("runtime.runtime.record_activity", fake_record)
    driver = FakeDriver(str(tmp_path))
    monkeypatch.setattr("runtime.runtime.get_sandbox_driver", lambda: driver)
    monkeypatch.setattr("runtime.runtime.SESSION_ROOT_OVERRIDE", str(tmp_path))

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        await NanoclawRuntime().execute(
            db, agent=agent, task_text="erste Runde", tenant_id=tenant, run_id=run_id
        )
        first = list(recorded)
        # The second leg: the same run, the same session folder, a new message.
        await NanoclawRuntime().execute(
            db, agent=agent, task_text="zweite Runde", tenant_id=tenant, run_id=run_id
        )

    assert first, "the first leg streamed something"
    assert recorded[: len(first)] == first
    assert len(recorded) == len(first) + 1, "the resume leg streams only what is new"


async def test_the_resumed_harness_is_told_exactly_which_action_was_approved(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The harness wakes on a message, and on a resume that message must be the
    decision -- naming the tool and its arguments, so the model reproduces the
    exact call the gateway has pre-decided. Sending the original task again would
    make it redo the reads it already did, and might produce different arguments,
    which the pre-decision would then refuse."""
    from runtime.runtime import NanoclawRuntime

    driver = FakeDriver(str(tmp_path))
    monkeypatch.setattr("runtime.runtime.get_sandbox_driver", lambda: driver)
    monkeypatch.setattr("runtime.runtime.SESSION_ROOT_OVERRIDE", str(tmp_path))

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.context = {
            **run.context,
            "resolved_tool_approvals": [
                {
                    "sig": "create_record\n{}",
                    "tool": "create_record",
                    "arguments": {"model": "crm.lead", "values": {"expected_revenue": 7500}},
                    "decision": "approve",
                }
            ],
        }
        await db.flush()
        await NanoclawRuntime().execute(
            db,
            agent=agent,
            task_text="Lege ein Angebot über 7500 an",
            tenant_id=tenant,
            run_id=run_id,
        )

    session = session_dir(str(tmp_path), agent.id, run_id)
    with sqlite3.connect(f"{session}/inbound.db") as conn:
        texts = [
            json.loads(r[0])["text"]
            for r in conn.execute("SELECT content FROM messages_in ORDER BY seq")
        ]
    assert any("FREIGEGEBEN" in t and "create_record" in t and "7500" in t for t in texts)
    assert not any(t == "Lege ein Angebot über 7500 an" for t in texts), (
        "the resume leg sends the decision, not the original task again"
    )


async def test_a_rejected_action_is_named_as_rejected(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from runtime.runtime import NanoclawRuntime

    driver = FakeDriver(str(tmp_path))
    monkeypatch.setattr("runtime.runtime.get_sandbox_driver", lambda: driver)
    monkeypatch.setattr("runtime.runtime.SESSION_ROOT_OVERRIDE", str(tmp_path))

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.context = {
            **run.context,
            "resolved_tool_approvals": [
                {
                    "sig": "create_record\n{}",
                    "tool": "create_record",
                    "arguments": {"model": "crm.lead"},
                    "decision": "reject",
                }
            ],
        }
        await db.flush()
        await NanoclawRuntime().execute(
            db, agent=agent, task_text="Lege ein Angebot an", tenant_id=tenant, run_id=run_id
        )

    session = session_dir(str(tmp_path), agent.id, run_id)
    with sqlite3.connect(f"{session}/inbound.db") as conn:
        texts = [
            json.loads(r[0])["text"]
            for r in conn.execute("SELECT content FROM messages_in ORDER BY seq")
        ]
    assert any("ABGELEHNT" in t for t in texts)


async def test_a_resume_leg_does_not_re_park_on_the_previous_legs_stale_marker(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A resumed run's context still carries `isolated_result` from the
    PREVIOUS leg's park -- `backend/src/oc8/api/mcp_gateway.py` writes it before
    it waits, and `backend/src/oc8/runtime/approval_resume.py` never clears it
    on decision. `_held_for_approval`'s very first poll -- before the container
    has had any chance to boot, let alone act -- re-reads that stale marker and
    immediately re-parks the run on the SAME old message, without ever giving
    the approved (or rejected) call a chance to execute.

    Live-observed 2026-07-27 against the real nanoclaw runtime and the real
    Odoo CRM (task-4 of the approval-path verification): an operator approved a
    held >3000EUR `create_record`, a fresh container spun up against the same
    session folder exactly as designed, but it was torn down within about a
    second every time -- before making a single call back to the control plane
    -- and the run silently fell back to `waiting_for_approval` with the
    original park's own text. No new approval was raised and nothing failed
    loudly; the run just looked like it was still, legitimately, waiting.
    """
    from runtime.runtime import NanoclawRuntime

    # ScriptedDriver, not FakeDriver: FakeDriver's `_play_container` writes the
    # ack SYNCHRONOUSLY inside `provision()`, before `_drive()`'s loop ever
    # runs -- `_sweep()` (checked before `_held_for_approval()`) would then win
    # the very first iteration regardless of the stale marker, masking the bug.
    # A real container needs real wall-clock time to boot and answer; scripting
    # the answer to land AFTER the loop's first iteration is what actually
    # exercises the race between the stale marker and the container's own work.
    # 0.25s against a 0.01s poll: the answer must land AFTER the loop's first
    # iteration, and the margin has to survive a loaded full-suite run -- at
    # 0.05s this test failed once in a full run and passed alone, which is a
    # knife-edge, not a signal. A stale marker still parks on iteration one,
    # ~250ms before the answer, so the property under test is unchanged.
    driver = ScriptedDriver(str(tmp_path), step_s=0.25)
    monkeypatch.setattr("runtime.runtime.get_sandbox_driver", lambda: driver)
    monkeypatch.setattr("runtime.runtime.SESSION_ROOT_OVERRIDE", str(tmp_path))
    monkeypatch.setattr("runtime.runtime._POLL_SECONDS", 0.01)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.context = {
            **run.context,
            "isolated_result": {
                "status": "waiting_for_approval",
                "output": "value €7500 meets threshold €3000",
            },
            "resolved_tool_approvals": [
                {
                    "sig": "create_record\n{}",
                    "tool": "create_record",
                    "arguments": {"model": "crm.lead", "values": {"expected_revenue": 7500}},
                    "decision": "approve",
                }
            ],
        }
        await db.flush()
        result = await NanoclawRuntime().execute(
            db,
            agent=agent,
            task_text="Lege ein Angebot über 7500 an",
            tenant_id=tenant,
            run_id=run_id,
        )

    assert result.status == "done", (
        "the resume leg must let the container attempt the approved call, not "
        "instantly re-park on the previous leg's already-resolved marker "
        f"(got status={result.status!r}, output={result.output!r})"
    )
    assert result.output == "Erledigt.", "the container's real answer, not the stale park text"


async def test_an_answer_that_merely_says_gateway_is_not_a_gateway_error() -> None:
    """The detector must spot THIS control plane's error text, not the word.

    Live, 2026-07-27: a lead named "Gateway Freigabe T8-1" was created in Odoo,
    the agent reported that correctly, and the run was recorded `failed` -- the
    mark `"gateway "` matched the lead's own name. Any answer mentioning a
    gateway, a payment gateway included, was a false failure.
    """
    assert not _is_gateway_error(
        'Der Lead "Gateway Freigabe T8-1" mit einem erwarteten Umsatz von 7.600 € '
        "wurde erfolgreich in Odoo angelegt (ID: 49)."
    )
    assert not _is_gateway_error("Ich habe das Payment Gateway des Kunden dokumentiert.")


async def test_the_harnesss_delivered_upstream_error_is_still_caught() -> None:
    """The real text observed live, which the detector exists for: the harness
    hands our own 5xx to the operator as if it were the agent's answer."""
    assert _is_gateway_error(
        'API Error: 502 {"detail":"400 from https://ai.opaas.online/v1/chat/completions: '
        '{\\"error\\":{\\"message\\":\\"litellm.BadRequestError\\"}}"}. This is a '
        "server-side issue, usually temporary — try again in a moment. If it "
        "persists, check your inference gateway (backend:8099)."
    )
    assert _is_gateway_error("upstream_error while calling the model")
    assert _is_gateway_error("POST /llm/v1/messages returned 502")


async def test_every_wake_message_carries_the_delivery_reminder(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Not just resume legs.

    The reminder was appended only when a run resumed after an approval, on the
    theory that the standing instructions already say it. Live, twice, they were
    not enough: a task text that arrives with its own step-by-step flow becomes
    the NEARER instruction and the model follows it instead. Both times the work
    itself was done correctly and the run was recorded `failed` -- the report
    never reached anyone because it was written as plain text.

    Most recently the follow-up run of a human decision, whose instruction is
    written by core (which cannot mention this runtime's delivery convention
    without leaking into every other runtime). So the reminder belongs on every
    wake message, appended here, where the convention actually lives.
    """
    from runtime.runtime import NanoclawRuntime

    driver = FakeDriver(str(tmp_path))
    monkeypatch.setattr("runtime.runtime.get_sandbox_driver", lambda: driver)
    monkeypatch.setattr("runtime.runtime.SESSION_ROOT_OVERRIDE", str(tmp_path))

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        await NanoclawRuntime().execute(
            db,
            agent=agent,
            task_text="Ein Mensch hat entschieden: voll erstatten. Setze das um.",
            tenant_id=tenant,
            run_id=run_id,
        )

    session = session_dir(str(tmp_path), agent.id, run_id)
    with sqlite3.connect(f"{session}/inbound.db") as conn:
        texts = [
            json.loads(r[0])["text"]
            for r in conn.execute("SELECT content FROM messages_in ORDER BY seq")
        ]
    assert texts, "the harness must have been woken at all"
    assert all("voll erstatten" in t for t in texts), "the instruction itself must survive"
    assert all("<message to=" in t for t in texts), (
        "every wake message has to say how an answer is delivered"
    )


async def test_a_silent_finish_after_real_work_is_done_not_failed(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """ "Nothing was said" and "nothing was done" are not the same thing.

    Live, 2026-07-28: a run opened ticket #61, posted three customer replies and
    moved its stage -- then the model returned an empty final turn, and the run
    was recorded `failed` with "the harness produced no message". In Odoo the
    customer had their answer. An operator reading the board goes looking for
    work that is already finished.

    This was FAILED at first, on the grounds that a run which cannot report is
    defective. A consequence that did not exist then now outweighs it: an
    operator who sees `failed` on a ticket run re-runs it, a re-run opens a NEW
    task, and the one-message-per-recipient guard is scoped to a task — so the
    customer gets a second answer. Calling finished work "failed" invites the
    exact duplicate that guard exists to prevent.

    So it is DONE, with the report reconstructed from what oc8 recorded and
    marked as oc8's own words, because an operator must be able to tell a report
    the agent wrote from one this system wrote after it fell silent.
    """
    from runtime.runtime import NanoclawRuntime

    driver = FakeDriver(str(tmp_path), reply=None)
    _install(monkeypatch, driver, str(tmp_path))

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        # What the run did before falling silent: a write it recorded for
        # idempotency, and the record its task card ended up naming.
        task = m.Task(
            tenant_id=tenant,
            department_id=agent.department_id,
            assigned_agent_id=agent.id,
            title="Ticket-Eingang",
            state="in_progress",
            meta_label="Bearbeitet Ticket #61",
        )
        db.add(task)
        await db.flush()
        run.task_id = task.id
        db.add(
            m.ToolInvocation(
                tenant_id=tenant,
                task_id=task.id,
                tool="update_record",
                args_hash="deadbeef",
                result="ok",
            )
        )
        await db.flush()

        result = await NanoclawRuntime().execute(
            db, agent=agent, task_text="Bearbeite das Ticket", tenant_id=tenant, run_id=run_id
        )

    assert result.status == "done", "the work happened; only the sentence is missing"
    assert "Bearbeitet Ticket #61" in result.output, "the report must name what was done"
    assert "1" in result.output, "and how much of it"
    assert "von oc8 zusammengefasst" in result.output, (
        "and must not pass itself off as the agent's own words"
    )


async def test_a_silent_finish_with_no_work_still_reads_as_nothing_happened(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The original signature must survive: a harness that could not deliver
    anything AND did nothing is a broken run, and must not be dressed up."""
    from runtime.runtime import NanoclawRuntime

    driver = FakeDriver(str(tmp_path), reply=None)
    _install(monkeypatch, driver, str(tmp_path))

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        result = await NanoclawRuntime().execute(
            db, agent=agent, task_text="Erstelle ein Angebot", tenant_id=tenant, run_id=run_id
        )

    assert result.status == "failed"
    assert "Aktion" not in result.output


async def test_a_team_lead_in_a_container_is_told_who_its_colleagues_are(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`delegate_task` takes a real agent id, so a lead that is never told one
    cannot use the tool it is offered -- every call is denied as invalid.

    In-process this was fixed long ago; here the standing instructions were built
    from system_prompt + catalog + delivery only, and the roster fell out. So a
    lead was strictly weaker in a container than the same lead in-process, with
    nothing in the log to say why. Observed live 2026-07-29: an accepted handoff
    told Sina to decompose and delegate, and her instructions never mentioned
    that Jan exists.
    """
    from runtime.runtime import NanoclawRuntime

    driver = FakeDriver(str(tmp_path))
    monkeypatch.setattr("runtime.runtime.get_sandbox_driver", lambda: driver)
    monkeypatch.setattr("runtime.runtime.SESSION_ROOT_OVERRIDE", str(tmp_path))

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        lead, run_id = await _agent_and_run(db, tenant)
        lead.is_team_lead = True
        mate = m.Agent(
            tenant_id=tenant,
            department_id=lead.department_id,
            name="Jan",
            status="idle",
            narrowing={},
            definition={},
            presentation={},
        )
        db.add(mate)
        await db.flush()
        await NanoclawRuntime().execute(
            db, agent=lead, task_text="Zerlege den Auftrag.", tenant_id=tenant, run_id=run_id
        )

    claude_md = _read(group_dir(str(tmp_path), lead.id, run_id), "CLAUDE.md")
    assert str(mate.id) in claude_md, "the id delegate_task needs, not just a name"
    assert "Jan" in claude_md


def test_an_answer_written_as_plain_text_is_recovered_not_thrown_away() -> None:
    """A run that does its whole job in one final turn and writes the result as
    plain text has done the work and lost only the envelope.

    Twice the answer to that was another reminder -- in the standing
    instructions, then on every wake message. Observed live 2026-07-29 with both
    in place: a lead decomposed an incoming order in its last turn, wrote it as
    plain text, and the run was recorded `failed` with nothing to show. oc8 reads
    the transcript instead of reprimanding the model.
    """
    import json as _json
    import tempfile
    from pathlib import Path as _Path

    from runtime.runtime import _unsent_answer

    with tempfile.TemporaryDirectory() as session:
        proj = _Path(session) / "claude-home" / "projects" / "-workspace-agent"
        proj.mkdir(parents=True)

        def turn(*parts: dict[str, str]) -> str:
            return _json.dumps({"type": "assistant", "message": {"content": list(parts)}})

        (proj / "s.jsonl").write_text(
            "\n".join(
                [
                    turn({"type": "text", "text": "Ich sehe zuerst im Ticketsystem nach."}),
                    turn({"type": "tool_use", "name": "search_records"}),
                    turn({"type": "text", "text": "Teilaufgabe 1: Vertrag anlegen."}),
                ]
            )
            + "\n"
        )
        got = _unsent_answer(session)

    assert got is not None
    assert "Teilaufgabe 1" in got, "the LAST turn, which is the report"
    assert "zuerst im Ticketsystem" not in got, "not the working notes before it"
    assert got.startswith("[von oc8"), "an operator must see this was fetched, not sent"


def test_thinking_the_model_marked_private_is_never_recovered() -> None:
    """`<internal>` is how this harness lets a model say "do not send this".
    Delivering it because the run would otherwise fail would publish exactly what
    was withheld."""
    import json as _json
    import tempfile
    from pathlib import Path as _Path

    from runtime.runtime import _unsent_answer

    with tempfile.TemporaryDirectory() as session:
        proj = _Path(session) / "claude-home" / "projects" / "-workspace-agent"
        proj.mkdir(parents=True)
        (proj / "s.jsonl").write_text(
            _json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "<internal>Der Kunde wirkt unglaubwürdig.</internal>",
                            }
                        ]
                    },
                }
            )
            + "\n"
        )
        assert _unsent_answer(session) is None


def test_no_transcript_is_not_an_error() -> None:
    """This runs on a path that is already failing; it must never add one."""
    import tempfile

    from runtime.runtime import _unsent_answer

    with tempfile.TemporaryDirectory() as session:
        assert _unsent_answer(session) is None


async def test_the_skill_catalogue_names_the_tool_the_model_can_actually_call(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A tool's name is not the same everywhere. In-process the model calls
    `skill_x`; through this bridge the same tool is `mcp__oc8__skill_x`.

    The catalogue named the bare form, and the model could not find it. Observed
    live 2026-07-29: a lead read "(skill_auftrag_zerlegen)", failed to find it,
    and reached for the harness's own generic `Skill` tool -- which does not
    reach oc8 at all. The procedure never loaded, so the work it describes never
    happened, and nothing in the run said why.
    """
    from runtime.runtime import NanoclawRuntime

    driver = FakeDriver(str(tmp_path))
    monkeypatch.setattr("runtime.runtime.get_sandbox_driver", lambda: driver)
    monkeypatch.setattr("runtime.runtime.SESSION_ROOT_OVERRIDE", str(tmp_path))

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        await _assign_skill(db, tenant, agent)
        await NanoclawRuntime().execute(
            db, agent=agent, task_text="Prüfe die Rechnung.", tenant_id=tenant, run_id=run_id
        )

    claude_md = _read(group_dir(str(tmp_path), agent.id, run_id), "CLAUDE.md")
    assert f"{MCP_TOOL_PREFIX}skill_" in claude_md, "the name as the bridge exposes it"
    assert "(skill_" not in claude_md, "never the bare form, which is not callable here"


async def test_the_harness_own_tools_are_denied_to_an_oc8_agent(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`Agent`/`Task` spawn a worker inside the container that inherits no frame,
    spends no budget anybody counts and appears in no audit trail -- the exact
    ungoverned worker oc8's permission model exists to prevent. `SendMessage` and
    `Skill` are dead ends the model cannot tell apart from the real thing.

    Naming them in the instructions worked, and only while the model reads
    carefully. A tool that is not offered cannot be reached for.
    """
    from runtime.runtime import NanoclawRuntime
    from runtime.session import DENIED_HARNESS_TOOLS, claude_home_dir

    driver = FakeDriver(str(tmp_path))
    monkeypatch.setattr("runtime.runtime.get_sandbox_driver", lambda: driver)
    monkeypatch.setattr("runtime.runtime.SESSION_ROOT_OVERRIDE", str(tmp_path))

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        await NanoclawRuntime().execute(
            db, agent=agent, task_text="Wie viele Leads?", tenant_id=tenant, run_id=run_id
        )

    import json as _json

    path = Path(claude_home_dir(str(tmp_path), agent.id, run_id)) / "settings.json"
    denied = _json.loads(path.read_text())["permissions"]["deny"]
    for tool in DENIED_HARNESS_TOOLS:
        assert tool in denied, tool


def test_the_harness_question_tool_is_denied_in_the_form_the_harness_honours() -> None:
    """Measured 2026-08-02: Sina got her oc8 skill and then called
    `mcp__nanoclaw__ask_user_question`, which blocks the container up to 300s
    polling inbound.db for an answer oc8 never writes. The run failed after 32s
    with "the agent container stopped responding" (session
    019fa4f7-.../019fc314-...); two of the retained sessions died the same way.

    The deny entry has to carry the FULL `mcp__<server>__<tool>` name: the CLI
    matches an MCP tool by that name, never by its bare one, so `Skill`-style
    bare entries would match nothing and this defect would ship again behind a
    comment claiming it was fixed.
    """
    import json as _json
    import tempfile
    from pathlib import Path as _Path

    from runtime.session import claude_home_dir, write_claude_settings

    agent_id, run_id = uuid.uuid4(), uuid.uuid4()
    with tempfile.TemporaryDirectory() as root:
        home = _Path(claude_home_dir(root, agent_id, run_id))
        home.mkdir(parents=True)
        write_claude_settings(root, agent_id, run_id)
        denied = _json.loads((home / "settings.json").read_text())["permissions"]["deny"]

    assert "mcp__nanoclaw__ask_user_question" in denied
    assert "ask_user_question" not in denied, "the bare name matches no MCP tool"


def test_the_original_four_denials_still_hold() -> None:
    """The list this widens started as these four (b97c8ae): two sub-agent holes
    and two dead ends. Widening it must not quietly drop one."""
    from runtime.session import DENIED_HARNESS_TOOLS

    for tool in ("Agent", "Task", "SendMessage", "Skill"):
        assert tool in DENIED_HARNESS_TOOLS, tool


def test_the_tools_the_agent_actually_works_with_are_still_offered() -> None:
    """The other half of the deny list: what must stay reachable.

    `WaitForMcpServers` is a harness BUILT-IN, not an `mcp__nanoclaw__` tool --
    the readiness instruction tells every agent to call it, and a blanket
    `mcp__nanoclaw__*` would be safe for it only by accident. `TaskOutput` and
    `TaskStop` read like companions to the denied `Task` but are the CLI's
    canonical names for reading and killing a background `Bash`.
    `mcp__nanoclaw__send_message` and `send_card` really do reach the operator
    (`messages.to_agent_message` has a branch for each). `mcp__oc8__*` is the
    bridge -- every tool the agent has a job to do with.

    Matching mirrors the CLI's own rule semantics, so a future `mcp__oc8__*` or
    bare-server entry fails here rather than silently in production.
    """
    from fnmatch import fnmatch

    from runtime.session import DENIED_HARNESS_TOOLS, REQUIRED_HARNESS_TOOLS

    for needed in REQUIRED_HARNESS_TOOLS:
        for rule in DENIED_HARNESS_TOOLS:
            server = needed.split("__")[1] if needed.startswith("mcp__") else None
            assert not fnmatch(needed, rule), f"{rule} denies {needed}"
            assert rule != f"mcp__{server}", f"{rule} denies the whole {server} server"


def test_no_deny_entry_names_a_tool_the_harness_does_not_have() -> None:
    """A deny entry that matches nothing is this defect shipped again behind a
    comment claiming it is fixed.

    The pinned CLI says so out loud -- measured 2026-08-02 by starting it with
    the previous list in `settings.json`: `Permission deny rule "TeamCreate"
    matches no known tool -- check for typos.`, once for `TeamCreate` and once
    for `TeamDelete`, on every leg. Both had been taken from the runner's
    `TOOL_ALLOWLIST`, which is a permission ALLOW list and not the tool surface.
    `Agent` is the one allowed exception: it is not offered under that name
    (`Task` is), but the CLI knows it as `Task`'s alias and does NOT warn on it.
    """
    from runtime.session import DENIED_HARNESS_TOOLS, HARNESS_TOOL_SURFACE

    unknown = [t for t in DENIED_HARNESS_TOOLS if t not in HARNESS_TOOL_SURFACE and t != "Agent"]
    assert unknown == [], f"denied but not offered by the pinned harness: {unknown}"


def test_every_required_tool_is_one_the_harness_actually_offers() -> None:
    """The same trap from the other side. Until 2026-08-02 this list "required"
    `TodoWrite` and `ToolSearch`; neither exists in the pinned CLI (they were
    superseded by `TaskCreate`/`TaskGet`/`TaskList`/`TaskUpdate`), so the guard
    was holding a line in front of nothing.

    `mcp__oc8__*` is exempt: that server is oc8's own bridge, not the harness.
    """
    from runtime.session import HARNESS_TOOL_SURFACE, REQUIRED_HARNESS_TOOLS

    missing = [
        t
        for t in REQUIRED_HARNESS_TOOLS
        if not t.startswith("mcp__oc8__") and t not in HARNESS_TOOL_SURFACE
    ]
    assert missing == [], f"required but not offered by the pinned harness: {missing}"


def test_every_offered_harness_tool_has_been_judged() -> None:
    """The first list denied four names and left twenty-six unexamined, which is
    how `mcp__nanoclaw__ask_user_question` survived to wedge Sina's run.

    So: each tool the pinned harness offers is either denied or deliberately
    kept, and the two sets have to cover the measured surface exactly. Adding a
    name to `HARNESS_TOOL_SURFACE` after a harness bump without deciding what it
    is fails here.
    """
    from runtime.session import DENIED_HARNESS_TOOLS, HARNESS_TOOL_SURFACE

    # Kept on purpose; the reasoning per tool is in session.py's comment block.
    kept = {
        "Bash",
        "Edit",
        "Glob",
        "Grep",
        "Read",
        "Write",
        "NotebookEdit",  # local file editing, same class as Edit
        "Monitor",  # background Bash with a notification channel; returns at once
        "TaskCreate",  # the CLI's own to-do list, TodoWrite's replacement
        "TaskGet",
        "TaskList",
        "TaskUpdate",
        "TaskOutput",  # canonical name for reading a background Bash
        "TaskStop",  # canonical name for killing one
        "WaitForMcpServers",  # the readiness workaround the preamble instructs
        "mcp__nanoclaw__send_message",  # really does reach the operator
        "mcp__nanoclaw__send_card",  # ditto, via messages._text_of
    }
    unjudged = set(HARNESS_TOOL_SURFACE) - set(DENIED_HARNESS_TOOLS) - kept
    assert unjudged == set(), f"offered by the harness and neither denied nor kept: {unjudged}"


def test_the_tools_that_look_like_a_way_out_are_denied() -> None:
    """Three ways an agent can believe it escaped this runtime when it did not.

    `Workflow` is `Task` with a fan-out multiplier -- "can spawn dozens of
    agents", in this container, under no oc8 frame, spending this run's token on
    oc8's own LLM gateway. It was missed the first time because it is not in the
    runner's `TOOL_ALLOWLIST`, which is not the tool surface.
    `PushNotification` claims to pull a human's attention to the session; there
    is no terminal and no Remote Control behind an agent container, and the tool
    pre-excuses its own silence ("If the result says the push wasn't sent,
    that's expected"). `WebFetch`/`WebSearch` are the open internet, which the
    `agents` docker network (`internal: true`) does not have -- and if it ever
    did, it would be a path around the SSRF-guarded fetcher.
    """
    from runtime.session import DENIED_HARNESS_TOOLS

    for tool in ("Workflow", "PushNotification", "WebFetch", "WebSearch"):
        assert tool in DENIED_HARNESS_TOOLS, tool


async def test_the_agent_is_told_the_one_way_to_reach_a_human(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Taking the question tool away without naming the open door is the worse
    bug: an agent that cannot ask guesses instead.

    The model is TOLD to reach for the wrong tool: every retained CLAUDE.md
    under `~/oc8-sessions/sessions/**` carries the tenant's own "Fehlt dir eine
    Information …, frag per `ask_user` nach". oc8's `ask_user` suspends the RUN
    and is deliberately not advertised over the tool gateway
    (`api/mcp_gateway.py` refuses every `CONTROL_TOOL_NAMES` member by name), so
    that instruction cannot be followed here at all. `request_decision` is on
    the gateway's core list for every agent and does not suspend the caller, so
    it is what the agent must be sent to -- and it has to be sent there by NAME,
    before it goes looking for the harness's own question tool.
    """
    from runtime.runtime import NanoclawRuntime

    driver = FakeDriver(str(tmp_path))
    monkeypatch.setattr("runtime.runtime.get_sandbox_driver", lambda: driver)
    monkeypatch.setattr("runtime.runtime.SESSION_ROOT_OVERRIDE", str(tmp_path))

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        await NanoclawRuntime().execute(
            db, agent=agent, task_text="Wie viele Leads?", tenant_id=tenant, run_id=run_id
        )

    claude_md = _read(group_dir(str(tmp_path), agent.id, run_id), "CLAUDE.md")
    assert f"{MCP_TOOL_PREFIX}request_decision" in claude_md, "the prefixed, callable name"
    assert "ask_user" in claude_md, "name the tool it will otherwise reach for"


#: The hook block the provisioner really writes, copied from a retained
#: `claude-home/settings.json`. The shape matters and this test used to invent a
#: shorter one: measured 2026-08-02, a `SessionStart` entry with no `hooks`
#: array makes the pinned CLI discard the WHOLE settings file -- the probe then
#: offers all 31 tools instead of 17, every denied tool back, with nothing on
#: stderr. A fixture in that shape is a fixture that says the deny list survives
#: a document the CLI would have thrown away. See `write_claude_settings`.
_PROVISIONER_HOOKS = {
    "SessionStart": [
        {
            "matcher": "startup|clear|compact",
            "hooks": [{"type": "command", "command": "bun /app/src/memory/hook.ts", "timeout": 10}],
        }
    ]
}


def test_denying_tools_keeps_what_the_provisioner_configured() -> None:
    """The provisioner puts nanoclaw's own memory hook in this file. Replacing it
    would take the agent's memory tree away to save a handful of tool names --
    and it has to survive INTACT, not merely present: the CLI validates the
    merged document as a whole and silently drops all of it, deny list included,
    if any part of it is malformed."""
    import json as _json
    import tempfile
    from pathlib import Path as _Path

    from runtime.session import claude_home_dir, write_claude_settings

    agent_id, run_id = uuid.uuid4(), uuid.uuid4()
    with tempfile.TemporaryDirectory() as root:
        home = _Path(claude_home_dir(root, agent_id, run_id))
        home.mkdir(parents=True)
        (home / "settings.json").write_text(_json.dumps({"hooks": _PROVISIONER_HOOKS}))
        write_claude_settings(root, agent_id, run_id)
        got = _json.loads((home / "settings.json").read_text())

    assert got["hooks"] == _PROVISIONER_HOOKS, "the memory hook survives unchanged"
    assert "Skill" in got["permissions"]["deny"]


async def test_the_agent_is_told_its_tools_may_still_be_starting(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The bridge to oc8 is a subprocess started alongside the model, and the
    model gets its first turn before the handshake finishes. Its tools answer
    "No such tool available … the MCP server 'oc8' is still starting", from which
    a model reasonably concludes it has NO tools and gives up -- reporting a
    configuration problem that does not exist.

    Observed live 2026-07-30, repeatedly. One run discovered `WaitForMcpServers`
    by itself, waited, and then worked normally; that is what the instruction
    makes reliable.
    """
    from runtime.runtime import NanoclawRuntime

    driver = FakeDriver(str(tmp_path))
    monkeypatch.setattr("runtime.runtime.get_sandbox_driver", lambda: driver)
    monkeypatch.setattr("runtime.runtime.SESSION_ROOT_OVERRIDE", str(tmp_path))

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run_id = await _agent_and_run(db, tenant)
        await NanoclawRuntime().execute(
            db, agent=agent, task_text="Wie viele Leads?", tenant_id=tenant, run_id=run_id
        )

    claude_md = _read(group_dir(str(tmp_path), agent.id, run_id), "CLAUDE.md")
    assert "WaitForMcpServers" in claude_md
    assert "No such tool available" in claude_md, "name the message it will actually see"
