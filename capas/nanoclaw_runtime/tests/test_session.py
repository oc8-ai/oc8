"""Session folder layout for the nanoclaw runtime.

The paths here are handed to the DOCKER DAEMON, so they must mean the same thing
on the host and inside the backend container -- see the spec's constraint 1.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

# capas/nanoclaw_runtime/tests/test_session.py -> parents[1] is the plugin root,
# which is the directory `runtime/` is imported from.
# See test_messages.py for why this is per-file rather than pytest's `pythonpath`.
# This file is MIXED -- module-level plugin imports just below AND function-local ones
# further down -- so it needs BOTH forms: the module-top prelude for collection time,
# and the `_plugin_path` autouse fixture for execution time. That is not redundant.
PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _evict() -> None:
    """Drop every cached `runtime`/`runtime.*` module from sys.modules.

    `runtime` is the package name EVERY `runtime_adapter` plugin now ships --
    four plugins behind one top-level module name (design §2) -- and sys.modules
    is keyed by NAME, not by path. Called SYMMETRICALLY on BOTH sides of the
    module-top import below AND on both sides of `_plugin_path`'s yield. See
    test_messages.py's copy for the full reasoning; the trailing half is the
    load-bearing one, because `loader.import_entry_point` only evicts modules IT
    ITSELF introduced.
    """
    for _stale in [n for n in sys.modules if n == "runtime" or n.startswith("runtime.")]:
        del sys.modules[_stale]


_evict()
sys.path.insert(0, str(PLUGIN_ROOT))

from runtime.session import (  # noqa: E402
    _chown_to_sandbox_user,
    agent_image,
    claude_home_dir,
    group_dir,
    platform_mounts,
    provisioner_image,
    session_dir,
    session_mounts_for,
    write_claude_md,
    write_container_json,
    write_routing,
)

from oc8.config import Settings, get_settings  # noqa: E402
from oc8.sandbox.mounts import validate_mounts  # noqa: E402

# Symmetric with the insert above -- nothing generic may be left cached for the
# next runtime plugin's test file (or for `loader.import_entry_point`).
_evict()
sys.path.remove(str(PLUGIN_ROOT))


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


def test_the_session_root_is_configurable_and_absolute() -> None:
    root = get_settings().runtime_session_root
    assert root.startswith("/"), "a relative root cannot be a host path"


def test_the_default_root_matches_the_compose_mount() -> None:
    # docker-compose.yml bind-mounts this path into the backend at the SAME
    # absolute path by default (${OC8_RUNTIME_SESSION_ROOT:-<this default>}).
    # Asserting the class default (not the resolved setting) keeps this test
    # honest even when a deployment overrides OC8_RUNTIME_SESSION_ROOT via
    # .env -- the two defaults must never drift apart from each other.
    assert Settings.model_fields["runtime_session_root"].default == "/var/lib/oc8/sessions"


def test_the_group_folder_lives_beside_the_session_it_belongs_to(tmp_path: Path) -> None:
    agent, run = uuid.uuid4(), uuid.uuid4()
    root = str(tmp_path)

    assert group_dir(root, agent, run) == os.path.join(session_dir(root, agent, run), "agent")


def test_a_session_is_not_inside_the_folder_the_container_can_write(tmp_path: Path) -> None:
    """The group folder is mounted read-write, so anything under it is reachable
    from inside the container. Sessions live elsewhere: one run must not be able
    to read, poison or forge the ack of another run of the same agent."""
    agent, run = uuid.uuid4(), uuid.uuid4()
    root = str(tmp_path)

    assert not session_dir(root, agent, run).startswith(group_dir(root, agent, run) + os.sep)


def test_two_runs_of_one_agent_do_not_share_a_session(tmp_path: Path) -> None:
    agent = uuid.uuid4()
    root = str(tmp_path)

    assert session_dir(root, agent, uuid.uuid4()) != session_dir(root, agent, uuid.uuid4())


def test_two_runs_of_one_agent_get_different_agent_folders(tmp_path: Path) -> None:
    """The group folder used to be shared across every run of an agent -- which is
    exactly how nanoclaw's own memory scaffold (written under
    /workspace/agent/memory at container boot, reloaded into context on every
    later run) turned into a second, un-gated persistence channel: one run's
    container could implant an instruction that a LATER run of the same agent
    would read back as its own standing memory. Making the folder per-run closes
    that: nothing one run writes under /workspace/agent can reach a different run
    of the same agent, because the two runs never share the folder."""
    agent = uuid.uuid4()
    root = str(tmp_path)

    assert group_dir(root, agent, uuid.uuid4()) != group_dir(root, agent, uuid.uuid4())


def test_container_json_names_one_mcp_server_and_no_token(tmp_path: Path) -> None:
    """The token is per RUN, and container.json is never deleted once written, so
    a token here would outlive the container process that used it."""
    group = str(tmp_path / "group")
    os.makedirs(group)

    path = write_container_json(
        group,
        assistant_name="Nora",
        group_name="Vertrieb (Odoo)",
        agent_id=uuid.UUID("00000000-0000-0000-0000-0000000000aa"),
        mcp_url="http://backend:8099/mcp",
    )
    cfg = json.loads(open(path).read())

    assert cfg["provider"] == "claude"
    assert cfg["assistantName"] == "Nora"
    assert list(cfg["mcpServers"]) == ["oc8"]
    assert cfg["mcpServers"]["oc8"]["command"] == "node"
    assert "model" not in cfg, "the gateway picks the model from the agent's ModelConfig"
    assert "token" not in json.dumps(cfg).lower()


def test_claude_md_carries_the_preamble(tmp_path: Path) -> None:
    group = str(tmp_path / "group")
    os.makedirs(group)
    path = write_claude_md(group, preamble="Du bist Nora.")
    assert "Du bist Nora." in open(path).read()


def test_claude_md_is_replaced_atomically_and_stays_private(tmp_path: Path) -> None:
    """Every leg of a resumed run rewrites this file, unconditionally, against
    the same per-run folder: a reader must see the whole old file or the whole
    new one, never a truncated middle."""
    group = str(tmp_path / "group")
    os.makedirs(group)
    write_claude_md(group, preamble="a" * 4096)

    path = write_claude_md(group, preamble="b" * 4096)

    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert open(path).read() == "b" * 4096
    # The temp file is renamed over the target, never left behind next to it.
    assert os.listdir(group) == ["CLAUDE.md"]


def test_the_session_mounts_are_writable_and_pass_the_allowlist(tmp_path: Path) -> None:
    agent, run = uuid.uuid4(), uuid.uuid4()
    root = str(tmp_path)
    os.makedirs(session_dir(root, agent, run), exist_ok=True)

    mounts = session_mounts_for(root, agent_id=agent, run_id=run)

    assert {m.container_path for m in mounts} == {
        "/workspace",
        "/workspace/agent",
        "/home/node/.claude",
    }
    assert all(not m.readonly for m in mounts)
    assert validate_mounts(mounts, allowed_root=root) == mounts


def test_the_claude_home_dir_lives_beside_the_session_it_belongs_to(tmp_path: Path) -> None:
    """Same reasoning as `group_dir`: per-RUN, not per-agent -- a resume leg is the
    SAME run, so per-run already carries everything the SDK's own resume needs,
    and a per-agent folder would reopen the un-gated persistence hole the
    per-run `group_dir` decision (session.py module docstring) already closed."""
    agent, run = uuid.uuid4(), uuid.uuid4()
    root = str(tmp_path)

    assert claude_home_dir(root, agent, run) == os.path.join(
        session_dir(root, agent, run), "claude-home"
    )


def test_two_runs_of_one_agent_get_different_claude_home_dirs(tmp_path: Path) -> None:
    """The mount that carries the Claude Code SDK's resume transcript must be
    keyed by run id, exactly like every other writable path here -- otherwise a
    later run of the same agent could read (or poison) an earlier run's
    session state."""
    agent = uuid.uuid4()
    root = str(tmp_path)

    assert claude_home_dir(root, agent, uuid.uuid4()) != claude_home_dir(root, agent, uuid.uuid4())


def test_the_claude_home_mount_is_writable_and_run_scoped(tmp_path: Path) -> None:
    """`/home/node/.claude` is where the Claude Code CLI stores its own resume
    state (settings.json, and -- critically -- the `.jsonl` transcript a
    resume leg reads back by session id). oc8 sets `HOME=/home/node` in the
    container env (runtime.py) but, without this mount, nothing under it
    survives the teardown between a park (for approval) and the fresh
    container that resumes it -- the SDK then cannot find its prior session,
    and an approved tool call can be silently lost (observed live,
    task-5-report.md in this same slice)."""
    agent, run = uuid.uuid4(), uuid.uuid4()
    root = str(tmp_path)
    os.makedirs(session_dir(root, agent, run), exist_ok=True)

    mounts = session_mounts_for(root, agent_id=agent, run_id=run)
    by_path = {m.container_path: m for m in mounts}
    claude_mount = by_path["/home/node/.claude"]

    assert not claude_mount.readonly
    assert claude_mount.host_path == claude_home_dir(root, agent, run)
    assert validate_mounts(mounts, allowed_root=root) == mounts


def test_the_platform_mounts_are_read_only_and_outside_the_session_root() -> None:
    """The runner/skills/bridge mounts are constants of the deployment; the two
    nested config mounts reuse `group`, a path the caller already puts through
    the session allowlist via `session_mounts_for` -- so none of these six need
    a second pass through it."""
    mounts = platform_mounts(
        runner_src="/opt/nanoclaw/agent-runner/src",
        skills_src="/opt/nanoclaw/skills",
        bridge_path="/opt/oc8/bridge.mjs",
        group="/srv/sessions/agents/a1",
        claude_home="/srv/sessions/agents/a1-home",
    )

    assert {m.container_path for m in mounts} == {
        "/app/src",
        "/app/skills",
        "/app/oc8-mcp-bridge.mjs",
        "/workspace/agent/container.json",
        "/workspace/agent/CLAUDE.md",
        # NOT /home/node/.claude/settings.json -- the runner writes it at
        # startup, see test_settings_json_is_writable_or_no_run_starts_at_all.
    }
    assert all(m.readonly for m in mounts)


def test_the_agents_own_config_is_mounted_read_only(tmp_path: Path) -> None:
    """A container that can rewrite its own CLAUDE.md writes standing instructions
    for its agent's FUTURE runs -- a persistence foothold out of one prompt."""
    mounts = platform_mounts(
        runner_src="/opt/nanoclaw/agent-runner/src",
        skills_src="/opt/nanoclaw/skills",
        bridge_path="/opt/oc8/bridge.mjs",
        group="/srv/sessions/agents/a1",
        claude_home="/srv/sessions/agents/a1-home",
    )
    by_path = {m.container_path: m for m in mounts}

    assert by_path["/workspace/agent/container.json"].readonly
    assert by_path["/workspace/agent/CLAUDE.md"].readonly
    assert by_path["/workspace/agent/container.json"].host_path == (
        "/srv/sessions/agents/a1/container.json"
    )
    assert by_path["/workspace/agent/CLAUDE.md"].host_path == "/srv/sessions/agents/a1/CLAUDE.md"


def test_settings_json_is_writable_or_no_run_starts_at_all() -> None:
    """This test asserts the OPPOSITE of what it did when it was written, and
    the reversal is the point.

    It was added to pin `settings.json` nested read-only, so an agent holding
    `Write` could not empty its own `permissions.deny` and be offered every
    denied tool on the next wake. The probe behind it was real and the CLI did
    honour the deny list with the file read-only.

    It was wrong about what runs BEFORE the CLI. The agent-runner opens this
    file for writing at startup, so with that mount EVERY run died 70 ms in:

        [agent-runner] Fatal error: EROFS: read-only file system,
        open '/home/node/.claude/settings.json'

    Measured 2026-08-02 against the pinned image (run 019fc39d), on the first
    real run after the change landed -- no test in this suite noticed, because
    none of them starts the runner.

    So the file stays writable and the bypass stays open. It is a hardening; a
    hardening that stops every run is worse than the hole it closes. The honest
    fix is to carry oc8's permissions in a settings source the runner does not
    write, which needs a mount and a cwd change and is its own slice.
    """
    mounts = platform_mounts(
        runner_src="/opt/nanoclaw/agent-runner/src",
        skills_src="/opt/nanoclaw/skills",
        bridge_path="/opt/oc8/bridge.mjs",
        group="/srv/sessions/agents/a1",
        claude_home="/srv/sessions/agents/a1/claude-home",
    )
    by_path = {m.container_path: m for m in mounts}

    assert "/home/node/.claude/settings.json" not in by_path

    # The directory it sits in is writable, which is what lets the runner start.
    agent, run = uuid.uuid4(), uuid.uuid4()
    home = session_mounts_for("/srv/sessions", agent_id=agent, run_id=run)
    assert not {m.container_path: m for m in home}["/home/node/.claude"].readonly


def test_routing_rows_exist_or_the_agents_answer_is_silently_dropped(tmp_path: Path) -> None:
    """Task 0's Q1: with no destination the harness completes the message and
    writes NOTHING -- the run would report success with an empty output."""
    session = str(tmp_path / "session")
    os.makedirs(session)
    sqlite3.connect(f"{session}/inbound.db").executescript(
        "CREATE TABLE destinations (name TEXT PRIMARY KEY, display_name TEXT, type TEXT NOT NULL,"
        " channel_type TEXT, platform_id TEXT, agent_group_id TEXT);"
        "CREATE TABLE session_routing (id INTEGER PRIMARY KEY CHECK (id = 1), channel_type TEXT,"
        " platform_id TEXT, thread_id TEXT);"
    )
    run = uuid.UUID("00000000-0000-0000-0000-0000000000bb")

    write_routing(session, run_id=run)

    with sqlite3.connect(f"{session}/inbound.db") as conn:
        dest = conn.execute("SELECT name, type, channel_type FROM destinations").fetchall()
        routing = conn.execute(
            "SELECT id, channel_type, platform_id FROM session_routing"
        ).fetchall()
    assert dest == [("operator", "channel", "oc8")]
    assert routing == [(1, "oc8", str(run))]


def test_the_image_names_come_from_the_environment_not_core_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """oc8.config stays software-neutral, so these vendor image names live in
    the plugin, read straight from the environment -- with a sane default so
    a bare dev checkout still works."""
    monkeypatch.delenv("OC8_NANOCLAW_AGENT_IMAGE", raising=False)
    monkeypatch.delenv("OC8_NANOCLAW_PROVISIONER_IMAGE", raising=False)
    assert agent_image() == "nanoclaw-agent:latest"
    assert provisioner_image() == "nanoclaw-provisioner:latest"

    monkeypatch.setenv("OC8_NANOCLAW_AGENT_IMAGE", "nanoclaw-agent:pinned")
    monkeypatch.setenv("OC8_NANOCLAW_PROVISIONER_IMAGE", "nanoclaw-provisioner:pinned")
    assert agent_image() == "nanoclaw-agent:pinned"
    assert provisioner_image() == "nanoclaw-provisioner:pinned"


def test_the_route_and_the_sender_name_the_same_party(tmp_path: Path) -> None:
    """One name, used on both legs: the destination the agent may answer to, and
    the sender it sees. Two words for the same party means the model addresses a
    destination that does not exist and its answer is silently dropped -- so this
    test fails if someone changes one side alone."""
    import sqlite3

    from runtime.session_db import CHANNEL_TYPE, ROUTE_NAME

    session = str(tmp_path / "session")
    os.makedirs(session)
    sqlite3.connect(f"{session}/inbound.db").executescript(
        "CREATE TABLE destinations (name TEXT PRIMARY KEY, display_name TEXT, type TEXT NOT NULL,"
        " channel_type TEXT, platform_id TEXT, agent_group_id TEXT);"
        "CREATE TABLE session_routing (id INTEGER PRIMARY KEY CHECK (id = 1), channel_type TEXT,"
        " platform_id TEXT, thread_id TEXT);"
    )
    run = uuid.uuid4()

    write_routing(session, run_id=run)

    with sqlite3.connect(f"{session}/inbound.db") as conn:
        name, channel = conn.execute("SELECT name, channel_type FROM destinations").fetchone()
        routed_channel = conn.execute("SELECT channel_type FROM session_routing").fetchone()[0]
    assert name == ROUTE_NAME
    assert channel == CHANNEL_TYPE == routed_channel


def test_the_mount_sources_come_from_the_deployment_not_from___file__(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plugin cannot derive a HOST path from its own location: inside the
    backend container `__file__` is /app/capas/..., which the docker daemon
    cannot mount, because it resolves paths on the host. Only the deployment
    knows where the pinned checkout lives (found live, 2026-07-27)."""
    from runtime.session import bridge_src, runner_src, skills_src

    assert runner_src().startswith("/") and "/app/capas" not in runner_src()

    monkeypatch.setenv("OC8_NANOCLAW_RUNNER_SRC", "/srv/nanoclaw/src")
    monkeypatch.setenv("OC8_NANOCLAW_SKILLS_SRC", "/srv/nanoclaw/skills")
    monkeypatch.setenv("OC8_NANOCLAW_BRIDGE_SRC", "/srv/oc8/bridge.mjs")
    assert (runner_src(), skills_src(), bridge_src()) == (
        "/srv/nanoclaw/src",
        "/srv/nanoclaw/skills",
        "/srv/oc8/bridge.mjs",
    )


async def test_provisioning_an_existing_session_does_not_run_the_provisioner_again(
    tmp_path: Path,
) -> None:
    """A resume leg opens the SAME session folder. Re-running nanoclaw's
    provisioner over live DBs would at best waste a container and at worst clobber
    the conversation the resume exists to continue."""
    from runtime.session import provision_session, session_dir

    agent, run = uuid.uuid4(), uuid.uuid4()
    session = session_dir(str(tmp_path), agent, run)
    os.makedirs(session)
    for name in ("inbound.db", "outbound.db"):
        sqlite3.connect(os.path.join(session, name)).close()

    class CountingDriver:
        provisioned = 0

        async def provision(self, spec: object) -> object:
            type(self).provisioned += 1
            raise AssertionError("the provisioner must not run for an existing session")

        async def wait(self, handle: object, timeout_s: float = 0) -> int:
            return 0

        async def logs(self, handle: object) -> str:
            return ""

        async def teardown(self, handle: object) -> None:
            return None

    driver = CountingDriver()
    returned = await provision_session(
        str(tmp_path), tenant_id=uuid.uuid4(), agent_id=agent, run_id=run, image="x", driver=driver
    )

    assert returned == session
    assert CountingDriver.provisioned == 0


async def test_provisioning_creates_the_claude_home_dir_for_the_sdks_own_resume_state(
    tmp_path: Path,
) -> None:
    """The mount in `session_mounts_for` is only as good as the directory it
    binds -- if nothing creates it, the docker daemon would create the bind
    SOURCE itself on first use (root-owned), and the sandbox uid could not
    write into it. Must exist (and be usable) BEFORE the first container ever
    starts, same as `session` and `group` already are."""
    from runtime.session import provision_session

    agent, run = uuid.uuid4(), uuid.uuid4()

    class NoopDriver:
        async def provision(self, spec: object) -> object:
            return object()

        async def wait(self, handle: object, timeout_s: float = 0) -> int:
            return 0

        async def logs(self, handle: object) -> str:
            return ""

        async def teardown(self, handle: object) -> None:
            return None

    await provision_session(
        str(tmp_path),
        tenant_id=uuid.uuid4(),
        agent_id=agent,
        run_id=run,
        image="x",
        driver=NoopDriver(),
    )

    claude_home = claude_home_dir(str(tmp_path), agent, run)
    # A one-off assertion read once the async work is over, same reasoning as
    # `provision_session`'s own os.path.exists check just above it.
    assert os.path.isdir(claude_home)  # noqa: ASYNC240


def test_chown_to_sandbox_user_is_a_no_op_when_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The default: sandbox containers keep the image's own user, and
    control-plane-owned files stay exactly as the control plane wrote them --
    this must never touch ownership when the operator has not opted in."""
    monkeypatch.delenv("OC8_SANDBOX_USER", raising=False)
    get_settings.cache_clear()
    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: calls.append((path, uid, gid)))

    _chown_to_sandbox_user(str(tmp_path))

    assert calls == []
    get_settings.cache_clear()


def test_a_chown_failure_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """EPERM (not running as root, restricted caps, a filesystem that refuses
    chown) is a real possibility. Swallowing it silently would recreate the
    exact half-working state this fix exists to close, only invisibly -- so a
    failed chown must be visible (a warning naming the path and uid) and must
    never abort the caller (provision_session / write_container_json /
    write_claude_md all run this inline on the happy path)."""
    monkeypatch.setenv("OC8_SANDBOX_USER", "1000:1000")
    get_settings.cache_clear()

    def _boom(path: str, uid: int, gid: int) -> None:
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(os, "chown", _boom)
    target = str(tmp_path / "container.json")
    # Alembic's fileConfig (run once per session by the `settings_env` fixture,
    # via alembic.ini's disable_existing_loggers default of True) disables any
    # logger that already exists at that point -- including this one, created
    # at collection time by this module's top-level `from runtime.session import
    # ...`. Undo that so caplog can actually observe records from it (same root
    # cause noted in tests/observability/test_setup.py and
    # tests/skills/test_runtime.py).
    #
    # The logger name is PINNED in session.py (`oc8.plugin.nanoclaw_runtime.session`)
    # rather than derived from `__name__`: `__name__` is now `runtime.session`, a
    # name all four runtime_adapter plugins would share. These two strings must
    # track that pin -- a stale one makes caplog capture nothing and this test
    # fails loudly on the `assert "chown" in caplog.text` below rather than
    # silently passing, which is why they are asserted on rather than tolerated.
    logging.getLogger("oc8.plugin.nanoclaw_runtime.session").disabled = False

    with caplog.at_level(logging.WARNING, logger="oc8.plugin.nanoclaw_runtime.session"):
        _chown_to_sandbox_user(target)  # must not raise

    assert "chown" in caplog.text.lower()
    assert target in caplog.text
    assert "1000:1000" in caplog.text
    get_settings.cache_clear()


async def test_the_provisioner_container_carries_the_organisation_label(tmp_path: Path) -> None:
    from oc8.sandbox.reaper import RUN_LABEL, TENANT_LABEL
    from runtime.session import provision_session

    agent, run, tenant = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    specs: list[object] = []

    class SpyDriver:
        async def provision(self, spec: object) -> object:
            specs.append(spec)
            return object()

        async def wait(self, handle: object, timeout_s: float = 0) -> int:
            return 0

        async def logs(self, handle: object) -> str:
            return ""

        async def teardown(self, handle: object) -> None:
            return None

    await provision_session(
        str(tmp_path), tenant_id=tenant, agent_id=agent, run_id=run, image="x", driver=SpyDriver()
    )

    labels = specs[0].labels  # type: ignore[attr-defined]
    assert labels[TENANT_LABEL] == str(tenant)
    assert labels[RUN_LABEL] == str(run)
