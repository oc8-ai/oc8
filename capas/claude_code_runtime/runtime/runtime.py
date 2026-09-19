"""Runs an agent's task through the Claude Code CLI in its own container.

One container per run leg (design doc §6, docs/superpowers/specs/
2026-08-12-multi-runtime-cli-agents-design.md): a suspend (approval or
ask_user, both parked by mcp_gateway.py) tears the container down and
persists Claude Code's own session id via cli_harness.session_state; the
next leg resumes that exact session with --resume instead of restarting the
task. Model calls go through oc8's own /llm gateway (ANTHROPIC_BASE_URL/
ANTHROPIC_AUTH_TOKEN, a run-scoped JWT standing in for a provider key,
mirroring nanoclaw_runtime's own pattern) and tool calls go directly to
oc8's /mcp gateway as a remote HTTP MCP server -- no bridge process, Claude
Code supports this transport natively (--mcp-config with "type": "http",
passed as a read-only FILE so the run's bearer JWT never rides on argv).

The session id alone does NOT make a resume work: Claude Code writes the
transcript that id names to `$CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/
<session>.jsonl`, i.e. OUTSIDE /workspace, and teardown destroys the
container filesystem. So the config dir is a per-run HOST directory mounted
read-write -- see cli_harness.dirs and nanoclaw_runtime/runtime/session.py's
`claude_home_dir`, which fixed the identical bug for nanoclaw first.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from cli_harness.dirs import ensure_dir, write_private_file
from cli_harness.mint import run_token
from cli_harness.outcome import no_terminal_event
from cli_harness.park import POLL_INTERVAL_S, is_parked
from cli_harness.session_state import clear_session_id, get_session_id, set_session_id
from cli_harness.tail import tail_new_lines
from cli_harness.toolchain import TOOLCHAIN_NOTE
from oc8 import models as m
from oc8.agent.engine import RunResult, open_run_task
from oc8.config import get_settings
from oc8.realtime.emit import publish_run_output_delta
from oc8.sandbox import get_sandbox_driver
from oc8.sandbox.mounts import validate_mounts
from oc8.sandbox.naming import container_name
from oc8.sandbox.reaper import RUN_LABEL
from oc8.sandbox.types import BindMount, SandboxSpec
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

_PLUGIN_NAME = "claude_code_runtime"
#: The container's HOME. `node`, not `oc8agent`: the base image is
#: node:22-bookworm-slim, which already ships a uid-1000 `node` user -- the
#: `useradd --uid 1000 oc8agent` this image used to carry failed the build
#: outright ("UID 1000 is not unique"). See Dockerfile.base. Set explicitly
#: in the container env because SandboxSpec.user pins a raw HOST uid that the
#: image's own passwd db does not know, and Docker then defaults HOME to "/".
_CONTAINER_HOME = "/home/node"
#: Where `claude` keeps its resumable transcripts. Named explicitly via
#: CLAUDE_CONFIG_DIR rather than left to fall back to $HOME/.claude, so the
#: mount below and the CLI cannot disagree about it.
_CLAUDE_CONFIG_DIR = f"{_CONTAINER_HOME}/.claude"
#: Where the MCP server definition lands INSIDE the container. Deliberately
#: NOT under _CLAUDE_CONFIG_DIR: that directory is mounted read-WRITE (the
#: resume transcript lives there), and this file carries the run's bearer
#: JWT and the /mcp endpoint -- keeping it on its own read-only mount, at a
#: path nested inside nothing writable, means the agent's own file-edit tools
#: never get a second, writable path to it. Same construction as
#: opencode_runtime's _OPENCODE_CONFIG_PATH.
_MCP_CONFIG_PATH = f"{_CONTAINER_HOME}/.config/oc8/mcp.json"
#: The run's real deadline (backend/src/oc8/sandbox/docker_driver.py's own
#: `wait()` docstring calls this out explicitly: a caller passing a SHORT
#: timeout_s -- as this loop does, POLL_INTERVAL_S -- must still track its
#: OWN overall bound and treat exceeding it as a failure once it does, since
#: docker_driver.py only raises once ITS timeout_s elapses, not this one).
#: A module-level global, not a local captured once per call, so a test can
#: `monkeypatch.setattr("runtime.runtime._RUN_TIMEOUT_S", ...)` -- the dotted
#: path is `runtime.runtime` because this package is now called `runtime/`
#: (the folder convention for every `runtime_adapter` plugin), which only
#: resolves to THIS plugin inside its test module's `_plugin_path` fixture.
#: and have the loop below actually observe the smaller value.
_RUN_TIMEOUT_S = 1800.0
#: Test-only override for the host root this run's directories are created
#: under. Mirrors codex_runtime/opencode_runtime (and
#: nanoclaw_runtime/runtime/runtime.py's SESSION_ROOT_OVERRIDE before them) exactly, and
#: for the same reason: creating the workspace and config-dir mount sources
#: before provision() means this plugin does REAL disk I/O even under a
#: FakeDriver, and the default runtime_session_root (/var/lib/oc8/sessions)
#: is neither writable nor expected to exist outside a deployed container.
SESSION_ROOT_OVERRIDE: str | None = None


def _workspace_dir(session_root: str, run_id: uuid.UUID) -> str:
    return f"{session_root}/{run_id}"


def _config_dir(session_root: str, run_id: uuid.UUID) -> str:
    return f"{session_root}/{run_id}-claude-config"


def _config_home_dir(session_root: str, run_id: uuid.UUID) -> str:
    """Backs `$CLAUDE_CONFIG_DIR` -- the transcript a `--resume` leg reads.

    Per-RUN and a SIBLING of /workspace's host directory, exactly like
    nanoclaw's `claude_home_dir`: a resume leg is the SAME run, so per-run
    already supplies everything the CLI's own resume needs, while a per-AGENT
    folder would let one run leave state a LATER, unrelated run of the same
    agent reads back as its own session.
    """
    return f"{session_root}/{run_id}-claude-home"


def _mcp_config(mcp_url: str, token: str) -> str:
    return json.dumps(
        {
            "mcpServers": {
                "oc8": {
                    "type": "http",
                    "url": mcp_url,
                    "headers": {"Authorization": f"Bearer {token}"},
                }
            }
        }
    )


def _write_mcp_config(*, session_root: str, run_id: uuid.UUID, mcp_url: str, token: str) -> str:
    """Write the MCP server definition to a HOST file and return its path, so
    the command line can NAME it instead of CARRYING it.

    `--mcp-config` takes "JSON files or strings" (verified against the CLI's
    own --help in this plugin's image, claude 2.1.229). Passing the string
    put the run's bearer JWT directly in the container's argv, where any
    `docker inspect` and any process on the host reading /proc/<pid>/cmdline
    could lift a live credential -- the one hole codex_runtime (bearer_token_
    env_var) and opencode_runtime (a config file) had both already closed by
    construction. A file closes it the same way, and 0600 + the sandbox-uid
    chown (`write_private_file`) keeps the host side no more readable than
    opencode's equivalent.
    """
    path = f"{_config_dir(session_root, run_id)}/mcp.json"
    return write_private_file(path, _mcp_config(mcp_url, token))


def _build_command(
    *, task_text: str, mcp_config_path: str, resume_session_id: str | None
) -> list[str]:
    # `--` (commander's end-of-flags separator) MUST come immediately before
    # task_text, and task_text MUST be last. `-p` is `--print`, a BOOLEAN
    # flag -- it does NOT consume the next token as its value, so task_text is
    # a BARE POSITIONAL (`claude [options] [command] [prompt]`), the same
    # shape codex_runtime and opencode_runtime already guard. MEASURED
    # against claude 2.1.229, not assumed: `claude -p --bare --output-format
    # json --nonexistent-flag-xyz` exits with "error: unknown option
    # '--nonexistent-flag-xyz'", while the same command with `--` before it
    # runs it as the prompt. So a task_text starting with "-" -- e.g.
    # literally "--dangerously-skip-permissions" -- is a real argument
    # injection here without this separator, not a theoretical one.
    cmd = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        # REQUIRED, not decorative: `claude` refuses outright with "When using
        # --print, --output-format=stream-json requires --verbose" (measured,
        # same build). Unlike --include-partial-messages (removed -- see
        # below) it does not multiply the output volume per token.
        "--verbose",
        "--bare",
        # A PATH, not the JSON itself -- the JSON carries this run's bearer
        # JWT and argv is world-readable. See _write_mcp_config. Safe ahead of
        # this variadic-capable flag's own consumption rules because a flag
        # (--permission-mode) follows it immediately, not the `--` separator.
        "--mcp-config",
        mcp_config_path,
        # Fully autonomous inside the container boundary, matching
        # codex_runtime's approval_policy="never" and opencode_runtime's
        # --auto. `acceptEdits` (what this used to pass) auto-accepts file
        # EDITS only: Bash and every other tool still raise a permission
        # request, and in headless -p mode with no permission-prompt-tool
        # configured there is nobody to answer one, so they are denied
        # outright. "bypassPermissions" is a documented value of this flag
        # (`--permission-mode` choices: acceptEdits, auto, bypassPermissions,
        # manual, dontAsk, plan -- verified against the CLI's own --help).
        # The governed surface is unaffected: oc8's own actions live behind
        # /mcp and still pass the PEP.
        "--permission-mode",
        "bypassPermissions",
    ]
    if resume_session_id is not None:
        cmd += ["--resume", resume_session_id]
    # NOTE: deliberately NOT passing --include-partial-messages. Nothing below
    # consumes a partial-message delta (see _parse_events' callers: only
    # system/init and result are read), and it emits a JSON event per token --
    # potentially many KB per 2s poll -- which is precisely what pushes the
    # terminal `result` line off the front of docker_driver.logs()'s 4000-char
    # trailing window before this loop can see it. See cli_harness.outcome.
    cmd += ["--", task_text]
    return cmd


def _parse_events(lines: list[str]) -> list[dict[str, Any]]:
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
    return events


async def _rebind(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Re-pin the RLS GUC after a commit -- it is transaction-local
    (`set_config(..., is_local=true)`, oc8.db.session.tenant_session), so
    every statement after a commit runs unbound otherwise and RLS fails
    closed: a `db.refresh(run)` right after the mid-loop commit below would
    find zero rows and raise, not silently see a stale one. Mirrors
    nanoclaw_runtime/runtime/runtime.py's NanoclawRuntime._rebind exactly, for the same
    reason."""
    await db.execute(
        sql_text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
    )


class ClaudeCodeRuntime:
    async def execute(
        self,
        db: AsyncSession,
        *,
        agent: m.Agent,
        task_text: str,
        tenant_id: uuid.UUID,
        run_id: uuid.UUID | None = None,
        mcp_conn: m.McpConnection | None = None,
        parent_task_id: uuid.UUID | None = None,
        delegation_depth: int = 0,
        cancel_check: Any = None,
        inbox_check: Any = None,
        pre_decided: dict[str, str] | None = None,
        originating_operator: str | None = None,
        # Accepted only to satisfy the RuntimeAdapter Protocol -- unused here
        # because the container never receives images through this call. It
        # reads them the same way it reads everything else about the run: off
        # `run_row.context["task_images"]`, exactly as runtime/isolated.py's
        # DockerIsolatedRuntime.execute() does for the other container-based
        # runtime.
        task_images_raw: list[dict[str, str]] | None = None,
    ) -> RunResult:
        assert run_id is not None, "claude_code_runtime requires a run row to correlate resumes"
        run = await db.get(m.AgentRun, run_id)
        assert run is not None

        # The container drives the run over the gateways and never creates
        # domain rows itself, so the control plane opens the Task here --
        # exactly as runtime/isolated.py does for the other container-based
        # runtime. Without it the run carries no task_id, the approval the
        # gateway raises carries none either, and resolve_tool_approval can
        # then never find the run to resume: an approved action would strand
        # the run in waiting_for_approval forever. (`run.task_id or
        # uuid.uuid4()` -- what this used to return -- fabricated an id no row
        # has ever had, which is worse than none.)
        task = await open_run_task(
            db,
            agent=agent,
            task_text=task_text,
            tenant_id=tenant_id,
            parent_task_id=parent_task_id,
            delegation_depth=delegation_depth,
            # A resume leg continues the suspended leg's task; see open_run_task.
            resume_task_id=run.task_id,
        )
        task_id = task.id
        run.task_id = task_id

        settings = get_settings()
        token = run_token(tenant_id=tenant_id, agent=agent, run_id=run_id)
        mcp_url = f"{settings.internal_base_url}/mcp"
        resume_session_id = get_session_id(run, _PLUGIN_NAME)
        session_root = SESSION_ROOT_OVERRIDE or settings.runtime_session_root

        # Both mount sources are created HERE, before provision(): Docker
        # auto-creates a missing bind-mount source as root:root, which the
        # container's unprivileged uid then cannot write to -- silently
        # defeating both /workspace and the resume transcript. See
        # cli_harness.dirs.
        workspace_path = ensure_dir(_workspace_dir(session_root, run_id))
        config_home_path = ensure_dir(_config_home_dir(session_root, run_id))
        mcp_config_path = _write_mcp_config(
            session_root=session_root, run_id=run_id, mcp_url=mcp_url, token=token
        )

        spec = SandboxSpec(
            image=settings.claude_code_agent_image,
            name=container_name(agent.name, "claude", run_id),
            # Identity for the startup reaper; the name is only for humans.
            labels={RUN_LABEL: str(run_id)},
            command=_build_command(
                task_text=f"{task_text}\n\n{TOOLCHAIN_NOTE}",
                mcp_config_path=_MCP_CONFIG_PATH,
                resume_session_id=resume_session_id,
            ),
            env={
                "ANTHROPIC_BASE_URL": f"{settings.internal_base_url}/llm",
                "ANTHROPIC_AUTH_TOKEN": token,
                "HOME": _CONTAINER_HOME,
                "CLAUDE_CONFIG_DIR": _CLAUDE_CONFIG_DIR,
            },
            network_disabled=False,
            network=settings.agent_runtime_network,
            # docker_driver.py documents this as a gate every caller must pass
            # its bind-mount paths through -- one allowed root, matched by
            # RESOLVED path, because `..` and a symlink both defeat a textual
            # prefix test. nanoclaw_runtime complies; this plugin did not.
            mounts=validate_mounts(
                [
                    BindMount(
                        host_path=workspace_path,
                        container_path="/workspace",
                        readonly=False,
                    ),
                    BindMount(
                        host_path=config_home_path,
                        container_path=_CLAUDE_CONFIG_DIR,
                        readonly=False,
                    ),
                    # ...and the MCP definition read-only, at a container path
                    # that is NOT nested inside that writable directory, so
                    # the agent cannot rewrite the endpoint it is governed
                    # through. Same shape as codex_runtime's config.toml and
                    # opencode_runtime's opencode.jsonc.
                    BindMount(
                        host_path=mcp_config_path,
                        container_path=_MCP_CONFIG_PATH,
                        readonly=True,
                    ),
                ],
                allowed_root=session_root,
            ),
            # Bind mounts carry HOST ownership, so the container has to run as
            # the uid that owns the directories created just above -- matched
            # by raw uid, not by an in-container username the image's passwd
            # db would have to know. See nanoclaw_runtime/runtime/session.py's
            # _chown_to_sandbox_user for the full reasoning.
            user=settings.sandbox_user or None,
        )

        driver = get_sandbox_driver()
        handle = await driver.provision(spec)
        # Unconditionally, immediately: release the executor's lock on the
        # agent_run row BEFORE the (possibly very slow) container runs. The
        # /mcp gateway updates that same row from its OWN session on every
        # tool call, and would otherwise block on this transaction's
        # uncommitted RUNNING transition for the whole run -- a deadlock the
        # executor cannot break, since it is itself waiting on the container.
        # runtime/isolated.py and nanoclaw_runtime/runtime/runtime.py both do exactly
        # this, and name the same deadlock. Committing only once a session id
        # appears (what this used to do) leaves the lock held for every run
        # that parks before its first event.
        await db.commit()
        await _rebind(db, tenant_id)

        started_at = time.monotonic()
        previous_raw = ""
        exit_code = 0
        # Every event seen across the whole poll loop, not just the latest
        # batch: the leg's `result` line typically arrives in the SAME
        # tail_new_lines() call whose iteration also observes the process
        # has exited (driver.wait() succeeding right after), so re-tailing
        # AFTER the loop -- against a previous_raw checkpoint that already
        # consumed that very line -- would see nothing new and misreport a
        # finished, successful run as failed. Accumulating avoids relying on
        # a second tail_new_lines() call (and a second driver.logs()) after
        # driver.teardown() has already torn the container down.
        all_events: list[dict[str, Any]] = []
        try:
            while True:
                new_lines, previous_raw = await tail_new_lines(
                    driver, handle, previous_raw=previous_raw
                )
                if new_lines:
                    await publish_run_output_delta(
                        tenant_id, run_id=run_id, chunk="\n".join(new_lines)
                    )
                new_events = _parse_events(new_lines)
                all_events.extend(new_events)
                for event in new_events:
                    if event.get("type") == "system" and event.get("subtype") == "init":
                        session_id = event.get("session_id")
                        if session_id and str(session_id) != resume_session_id:
                            # merge_context, NOT `run.context = ...`: the /mcp
                            # gateway may have just written the park marker
                            # from its own session, and a whole-object write
                            # built from this session's older snapshot would
                            # erase it -- leaving this loop spinning for the
                            # full 30-minute deadline on a run that HAS parked.
                            # See cli_harness.session_state.
                            await set_session_id(db, run, _PLUGIN_NAME, str(session_id))
                            await db.commit()
                            await _rebind(db, tenant_id)

                await db.refresh(run)
                parked, status = is_parked(run)
                if parked:
                    return RunResult(
                        task_id=task_id,
                        agent_id=agent.id,
                        status=status,
                        output=str((run.context.get("isolated_result") or {}).get("output", "")),
                    )

                # §7.2 operator cancel, probed every poll exactly as
                # nanoclaw_runtime/runtime/runtime.py does in its own drive loop: an
                # agent that cannot be stopped is the one failure mode an
                # operator has no answer to. The finally below tears the
                # container down on the way out, so returning here is enough.
                if cancel_check is not None and await cancel_check():
                    return RunResult(
                        task_id=task_id,
                        agent_id=agent.id,
                        status="interrupted",
                        output="Stopped by an operator.",
                    )

                # NOTE: `inbox_check` is deliberately NOT probed here, unlike
                # nanoclaw's loop. Calling it MARKS the messages delivered,
                # and a headless `claude -p` run has no channel to inject them
                # into once it has started -- draining them would silently
                # DESTROY operator steering messages rather than deliver them.
                # Wiring this up properly needs --input-format stream-json and
                # a stdin the sandbox driver does not currently expose; until
                # then, not consuming them leaves them for the next leg.

                # driver.wait()'s own timeout_s (POLL_INTERVAL_S below) is
                # deliberately short so this loop can also check park status
                # and elapsed time every couple seconds -- it is NOT the run's
                # deadline, and every "still running" iteration raises out of
                # driver.wait() (see the except below) rather than returning
                # normally. So the run's real deadline has to be enforced
                # here, independently, or a wedged `claude` process (stuck on
                # /llm, a CLI-side bug, a network partition to the gateway)
                # polls forever -- never surfacing as failed, never releasing
                # whatever worker slot is polling it.
                if time.monotonic() - started_at > _RUN_TIMEOUT_S:
                    return RunResult(
                        task_id=task_id,
                        agent_id=agent.id,
                        status="failed",
                        output=f"claude-code timed out after {_RUN_TIMEOUT_S:.0f}s",
                    )

                try:
                    exit_code = await driver.wait(handle, timeout_s=POLL_INTERVAL_S)
                    # One last tail BEFORE teardown, not after: each loop
                    # iteration tails logs THEN blocks in driver.wait() for
                    # up to POLL_INTERVAL_S, so a task that finishes within a
                    # single interval has its final `result` event written to
                    # stdout WHILE this call is blocked inside wait() -- after
                    # the tail that already ran this iteration, before any
                    # tail that would ever run again. The container is still
                    # present here (teardown is the very next line, in the
                    # finally below), so this is the safe side of the "second
                    # driver.logs() call after teardown()" bug the
                    # accumulator comment above warns about -- it stays a
                    # genuine bug to re-tail from OUTSIDE this loop, after
                    # teardown, not to take one more reading before it.
                    final_lines, previous_raw = await tail_new_lines(
                        driver, handle, previous_raw=previous_raw
                    )
                    if final_lines:
                        await publish_run_output_delta(
                            tenant_id, run_id=run_id, chunk="\n".join(final_lines)
                        )
                    all_events.extend(_parse_events(final_lines))
                    break
                except Exception:
                    continue  # still running; poll again
        finally:
            await driver.teardown(handle)

        result_event = next((e for e in all_events if e.get("type") == "result"), None)

        if exit_code == 0 and result_event is None:
            # Distinct from a task failure, and labelled as such: a clean exit
            # with no terminal event means the answer was lost, not that the
            # work failed. See cli_harness.outcome.
            return RunResult(
                task_id=task_id,
                agent_id=agent.id,
                status="failed",
                output=no_terminal_event("claude-code"),
            )

        if exit_code != 0 or result_event is None or result_event.get("subtype") != "success":
            return RunResult(
                task_id=task_id,
                agent_id=agent.id,
                status="failed",
                output=(result_event or {}).get("result", f"claude-code exited {exit_code}"),
            )

        session_id = result_event.get("session_id")
        if session_id:
            await clear_session_id(db, run, _PLUGIN_NAME)
            await db.commit()
            # The caller (oc8.runtime.executor) re-reads the run row on this
            # same `db` the instant execute() returns, so a commit here must
            # leave the session re-bound just as much as the mid-loop one.
            await _rebind(db, tenant_id)

        return RunResult(
            task_id=task_id,
            agent_id=agent.id,
            status="done",
            output=str(result_event.get("result", "")),
        )


def register(contrib: Any) -> None:
    # A CLASS, not an instance: PluginContributions.add_runtime is typed
    # `type[RuntimeAdapter]` and oc8.runtime.registry.resolve_runtime does
    # `impl()` on whatever it gets back (registry.py ~line 206) -- passing
    # an instance here would make that call try to instantiate an instance,
    # which fails the moment an agent is actually dispatched onto this
    # runtime. Mirrors nanoclaw_runtime/runtime/runtime.py's register, which passes its
    # own class the same way.
    contrib.add_runtime(ClaudeCodeRuntime)
