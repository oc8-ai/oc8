"""Runs an agent's task through the OpenAI Codex CLI in its own container.
See claude_code_runtime/runtime/runtime.py's module docstring for the shared design
(one container per run leg, oc8's gateways for model/tool calls, no bridge
process). Codex-specific: MCP servers and the approval/sandbox policy are
config.toml, not CLI flags (codex-rs/config/src/mcp_types.rs's
McpServerTransportConfig::StreamableHttp and codex-rs/protocol/src/
protocol.rs's AskForApproval::Never, both verified against Codex's own
source); resume is the `codex exec resume <SESSION_ID>` subcommand, not a
flag on the base command; and the session that subcommand resumes lives in
`$CODEX_HOME/sessions` (verified against a real Codex install's own
$CODEX_HOME layout), i.e. OUTSIDE /workspace -- so $CODEX_HOME is a per-run
HOST directory mounted read-write, or teardown destroys the very thing the
next leg resumes."""

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

_PLUGIN_NAME = "codex_runtime"
#: An arbitrary, non-empty label -- oc8's /llm gateway resolves the model
#: from the run-scoped token (OPENAI_API_KEY here), never from this field.
#: Matches opencode_runtime's identical `_MODEL_ID` constant/reasoning.
_MODEL_ID = "oc8-gateway"
#: The container's HOME. `node`, not `oc8agent`: the base image is
#: node:22-bookworm-slim, which already ships a uid-1000 `node` user -- the
#: `useradd --uid 1000 oc8agent` this image used to carry failed the build
#: outright ("UID 1000 is not unique"). See claude_code_runtime/
#: Dockerfile.base, which all three images share.
_CONTAINER_HOME = "/home/node"
_CODEX_HOME = f"{_CONTAINER_HOME}/.codex"
_CONFIG_PATH = f"{_CODEX_HOME}/config.toml"
_RUN_TIMEOUT_S = 1800.0
#: Test-only override for the host root this run's directories are created
#: under. Mirrors nanoclaw_runtime/runtime/runtime.py's SESSION_ROOT_OVERRIDE exactly, and
#: for the same reason: fixing the config.toml write-after-provision() race
#: (see _write_config_toml's docstring) and creating the mount sources before
#: provision() means this plugin does REAL disk I/O even under a FakeDriver,
#: and the default runtime_session_root (/var/lib/oc8/sessions) is neither
#: writable nor expected to exist outside a deployed container.
SESSION_ROOT_OVERRIDE: str | None = None
# Task 3's review (claude_code_runtime) found the poll loop below has NO
# overall deadline without this check: driver.wait(timeout_s=POLL_INTERVAL_S)
# raises every ~2s while the container is merely still running, and looping
# on that exception forever means a genuinely wedged CLI process (stuck /llm
# call, CLI bug, network partition) never resolves to "failed" -- see
# docker_driver.py's wait() docstring, which documents the timeout it's
# given as the bound meant to close a wedged container, and
# nanoclaw_runtime/runtime/runtime.py:690-707's own started_at/ends_at pattern for
# the established precedent. The elapsed-time check inside the loop below
# is what actually enforces this constant.


def _workspace_dir(session_root: str, run_id: uuid.UUID) -> str:
    return f"{session_root}/{run_id}"


def _codex_home_dir(session_root: str, run_id: uuid.UUID) -> str:
    """Backs `$CODEX_HOME` -- where `codex exec resume <id>` finds its session.

    Per-RUN and a SIBLING of /workspace's host directory, exactly like
    claude_code_runtime's `_config_home_dir` and nanoclaw's `claude_home_dir`
    before it: a resume leg is the SAME run, so per-run supplies everything
    the CLI's own resume needs, while a per-AGENT folder would let one run
    leave state a LATER, unrelated run of the same agent reads back as its
    own session.
    """
    return f"{session_root}/{run_id}-codex-home"


def _config_dir(session_root: str, run_id: uuid.UUID) -> str:
    return f"{session_root}/{run_id}-codex-config"


def _config_toml(*, mcp_url: str, llm_url: str) -> str:
    # OPENAI_BASE_URL alone does NOT redirect codex to oc8's gateway: its
    # default "openai" provider talks the Responses API over a HARDCODED
    # wss://api.openai.com/v1/responses websocket, ignoring OPENAI_BASE_URL
    # entirely -- observed live, every run failed with "codex exited 1" and
    # a DNS-lookup error against api.openai.com, no oc8 traffic at all,
    # until an explicit provider was added here.
    #
    # wire_api="chat" (the plain HTTP Chat Completions API oc8's gateway
    # actually implements) is NOT an option with the codex-cli version this
    # image carries -- it hard-errors at config load ("wire_api = chat is no
    # longer supported"). wire_api="responses" is accepted and DOES honor
    # base_url over plain HTTP (POST {base_url}/responses, not the
    # websocket -- that only fires for the hardcoded default provider), so
    # this gets a run all the way to oc8's own gateway -- but oc8's /llm
    # gateway has no /v1/responses route yet (only /v1/chat/completions and
    # /v1/messages), so every call 404s there until that route exists. Left
    # set to "responses" anyway: it is the version that actually reaches
    # oc8, producing that one missing-route 404 as the honest next blocker,
    # rather than "chat"'s earlier, less informative hard config-load error.
    return (
        'approval_policy = "never"\n'
        'sandbox_mode = "workspace-write"\n'
        'model_provider = "oc8"\n'
        f'model = "{_MODEL_ID}"\n'
        "\n"
        "[model_providers.oc8]\n"
        'name = "oc8"\n'
        f'base_url = "{llm_url}"\n'
        'env_key = "OPENAI_API_KEY"\n'
        'wire_api = "responses"\n'
        "\n"
        "[mcp_servers.oc8]\n"
        'type = "streamable_http"\n'
        f'url = "{mcp_url}"\n'
        'bearer_token_env_var = "OC8_TOKEN"\n'
    )


def _write_config_toml(
    *, session_root: str, run_id: uuid.UUID, mcp_url: str, llm_url: str
) -> str:
    """Write config.toml to a HOST path before the container starts, and
    return that path for the caller to bind-mount in -- NOT a driver.fs_write
    call into an already-running container.

    A review of this plugin found the original fs_write-after-provision()
    sequence racy: provision() starts the container (and, via the base
    image's tini entrypoint, `codex exec ...`) immediately, and fs_write is a
    SEPARATE Docker API round-trip with nothing synchronizing the two. If
    `codex` reads config.toml before fs_write lands, it starts under Codex's
    default INTERACTIVE approval policy instead of approval_policy="never" --
    and Codex's own documented non-interactive-mode behaviour is to fail the
    run outright the first time it would otherwise prompt. That defeats
    exactly what this plugin's config.toml exists to enforce, silently and
    non-deterministically (a race, not a guaranteed failure), which is why no
    test caught it against the (instantaneous) FakeDriver. A bind mount has
    no such race: the file exists on the host and is already present at
    container start, before Codex's entrypoint ever runs.

    Its own host directory, NOT the `$CODEX_HOME` directory it is mounted
    INTO. $CODEX_HOME must be writable (Codex writes its resumable session
    there -- see `_codex_home_dir`), and putting config.toml inside that same
    HOST directory would give the agent's own file-edit tools a writable path
    to the file that carries `approval_policy = "never"`, defeating the
    read-only mount that exists to stop exactly that. Two host directories,
    one read-write and one read-only, is what keeps them independent -- the
    same class of bypass nanoclaw_runtime/runtime/session.py's platform_mounts
    docstring documents for settings.json.
    """
    path = f"{_config_dir(session_root, run_id)}/config.toml"
    return write_private_file(path, _config_toml(mcp_url=mcp_url, llm_url=llm_url))


def _build_command(*, task_text: str, resume_session_id: str | None) -> list[str]:
    # `--` (clap's end-of-flags separator) MUST come immediately before
    # task_text, in both branches: task_text is free-form (a human chat
    # message, a delegated task, a folded-in clarification answer on
    # resume), and `codex exec`'s own arg parser (codex-rs/exec/src/cli.rs,
    # clap-based) takes the prompt as a bare positional with nothing
    # upstream of it that pins it to a flag's value. Without `--`, a
    # task_text that happens to start with `-` -- e.g. literally
    # "--dangerously-bypass-approvals-and-sandbox" -- parses as a FLAG, not
    # the prompt, defeating the approval_policy/sandbox_mode this whole
    # plugin's config.toml exists to enforce. The same guard is now in
    # opencode_runtime (`opencode run [message..]`) and, since the final
    # whole-branch review, claude_code_runtime too -- `claude`'s `-p` is
    # `--print`, a boolean, so ITS task_text is a bare positional as well.
    #
    # `--skip-git-repo-check` is NOT optional: /workspace is a bare bind mount
    # and `codex exec` refuses outright in a directory that is not a git repo
    # ("Not inside a trusted directory and --skip-git-repo-check was not
    # specified", exit 2 -- measured against codex-cli 0.147.0 inside this
    # plugin's own image). Codex's git check is a guard for a human running it
    # over their own working tree; here the container boundary is the
    # isolation, and the workspace is created fresh per run by this plugin.
    flags = ["--json", "--skip-git-repo-check"]
    if resume_session_id is not None:
        return ["codex", "exec", "resume", resume_session_id, *flags, "--", task_text]
    return ["codex", "exec", *flags, "--", task_text]


def _parse_events(lines: list[str]) -> list[dict[str, Any]]:
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
    return events


def _thread_id(events: list[dict[str, Any]]) -> str | None:
    started = next((e for e in events if e.get("type") == "thread.started"), None)
    return str(started["thread_id"]) if started and started.get("thread_id") else None


def _final_message(events: list[dict[str, Any]]) -> str | None:
    messages = [
        e["item"]["text"]
        for e in events
        if e.get("type") == "item.completed"
        and isinstance(e.get("item"), dict)
        and e["item"].get("type") == "agent_message"
    ]
    return messages[-1] if messages else None


async def _rebind(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Re-pin the RLS GUC after a commit -- it is transaction-local
    (`set_config(..., is_local=true)`, oc8.db.session.tenant_session), so
    every statement after a commit runs unbound otherwise and RLS fails
    closed: a `db.refresh(run)` right after the mid-loop commit below would
    find zero rows and raise, not silently see a stale one. Mirrors
    claude_code_runtime/runtime/runtime.py's _rebind (itself copied from
    nanoclaw_runtime/runtime/runtime.py's NanoclawRuntime._rebind) exactly, for the same
    reason."""
    await db.execute(
        sql_text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
    )


class CodexRuntime:
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
        assert run_id is not None, "codex_runtime requires a run row to correlate resumes"
        run = await db.get(m.AgentRun, run_id)
        assert run is not None

        # The control plane opens the Task, not the container -- see
        # claude_code_runtime/runtime/runtime.py's identical block (and
        # runtime/isolated.py, the precedent both follow) for why a
        # fabricated uuid4 task_id strands an approved run forever.
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
        # /v1 suffix matches opencode_runtime's identical llm_url and the
        # actual route this hits: backend/src/oc8/api/llm_gateway.py mounts
        # POST /v1/chat/completions under the /llm prefix, and codex's
        # wire_api="chat" provider (see _config_toml) appends /chat/completions
        # itself.
        llm_url = f"{settings.internal_base_url}/llm/v1"
        resume_session_id = get_session_id(run, _PLUGIN_NAME)
        session_root = SESSION_ROOT_OVERRIDE or settings.runtime_session_root

        # All three mount sources are created HERE, before provision():
        # Docker auto-creates a missing bind-mount source as root:root, which
        # the container's unprivileged uid then cannot write to -- silently
        # defeating both /workspace and the resumable session. See
        # cli_harness.dirs.
        workspace_path = ensure_dir(_workspace_dir(session_root, run_id))
        codex_home_path = ensure_dir(_codex_home_dir(session_root, run_id))
        config_toml_path = _write_config_toml(
            session_root=session_root, run_id=run_id, mcp_url=mcp_url, llm_url=llm_url
        )

        spec = SandboxSpec(
            image=settings.codex_agent_image,
            name=container_name(agent.name, "codex", run_id),
            # Identity for the startup reaper; the name is only for humans.
            labels={RUN_LABEL: str(run_id)},
            command=_build_command(
                task_text=f"{task_text}\n\n{TOOLCHAIN_NOTE}", resume_session_id=resume_session_id
            ),
            env={
                "OPENAI_BASE_URL": f"{settings.internal_base_url}/llm",
                "OPENAI_API_KEY": token,
                "OC8_TOKEN": token,
                "CODEX_HOME": _CODEX_HOME,
                # Explicit because SandboxSpec.user pins a raw HOST uid the
                # image's passwd db does not know, so Docker would otherwise
                # default HOME to "/".
                "HOME": _CONTAINER_HOME,
            },
            network_disabled=False,
            network=settings.agent_runtime_network,
            # docker_driver.py documents validate_mounts as a gate every
            # caller must pass its bind-mount paths through; nanoclaw_runtime
            # complies and this plugin did not.
            mounts=validate_mounts(
                [
                    BindMount(
                        host_path=workspace_path,
                        container_path="/workspace",
                        readonly=False,
                    ),
                    # $CODEX_HOME writable, so `codex exec resume <id>` on the
                    # next leg can still find the session this leg wrote.
                    BindMount(
                        host_path=codex_home_path,
                        container_path=_CODEX_HOME,
                        readonly=False,
                    ),
                    # ...and config.toml LAYERED read-only on top of it, at
                    # the exact path it would otherwise occupy inside that
                    # writable directory. Two entries whose container paths
                    # nest is fine here: docker_driver.py builds `volumes` as
                    # a dict keyed by HOST path (these are two different host
                    # paths, so neither shadows the other in the dict) and the
                    # daemon orders mount points by destination depth, so the
                    # deeper, read-only one wins for this one file. Written to
                    # disk BEFORE provision() -- see _write_config_toml.
                    BindMount(
                        host_path=config_toml_path,
                        container_path=_CONFIG_PATH,
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
        # agent_run row BEFORE the container runs -- see
        # claude_code_runtime/runtime/runtime.py's identical commit for the deadlock
        # this avoids (runtime/isolated.py and nanoclaw_runtime name it too).
        await db.commit()
        await _rebind(db, tenant_id)

        started_at = time.monotonic()
        previous_raw = ""
        exit_code = 0
        # Every event seen across the whole poll loop, not just the latest
        # batch -- see claude_code_runtime/runtime/runtime.py's identical comment on
        # its own all_events accumulator for why re-tailing AFTER the loop
        # (against a previous_raw checkpoint that may have already consumed
        # the final event line) would risk misreporting a finished,
        # successful run as failed.
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
                tid = _thread_id(new_events)
                if tid is not None and tid != resume_session_id:
                    # merge_context, NOT `run.context = ...` -- see
                    # claude_code_runtime/runtime/runtime.py's identical comment and
                    # cli_harness.session_state: a whole-object write from
                    # this session's older snapshot erases the park marker the
                    # /mcp gateway just committed from its own session.
                    await set_session_id(db, run, _PLUGIN_NAME, tid)
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
                # nanoclaw_runtime/runtime/runtime.py does in its own drive loop; the
                # finally below tears the container down on the way out.
                if cancel_check is not None and await cancel_check():
                    return RunResult(
                        task_id=task_id,
                        agent_id=agent.id,
                        status="interrupted",
                        output="Stopped by an operator.",
                    )

                # NOTE: `inbox_check` is deliberately NOT probed -- see
                # claude_code_runtime/runtime/runtime.py's identical note. Calling it
                # marks messages delivered, and a headless `codex exec` has no
                # channel to inject them into once started, so draining them
                # would destroy operator steering rather than deliver it.

                if time.monotonic() - started_at > _RUN_TIMEOUT_S:
                    return RunResult(
                        task_id=task_id,
                        agent_id=agent.id,
                        status="failed",
                        output=f"codex timed out after {_RUN_TIMEOUT_S:.0f}s",
                    )

                try:
                    exit_code = await driver.wait(handle, timeout_s=POLL_INTERVAL_S)
                    # One last tail BEFORE teardown, not after: each loop
                    # iteration tails logs THEN blocks in driver.wait() for
                    # up to POLL_INTERVAL_S, so a task that finishes within a
                    # single interval has its final terminal event written to
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

        message = _final_message(all_events)

        if exit_code == 0 and message is None:
            # Distinct from a task failure, and labelled as such: a clean exit
            # with no terminal event means the answer was lost, not that the
            # work failed. See cli_harness.outcome.
            return RunResult(
                task_id=task_id,
                agent_id=agent.id,
                status="failed",
                output=no_terminal_event("codex"),
            )

        if exit_code != 0 or message is None:
            return RunResult(
                task_id=task_id,
                agent_id=agent.id,
                status="failed",
                output=message or f"codex exited {exit_code}",
            )

        await clear_session_id(db, run, _PLUGIN_NAME)
        await db.commit()
        # The caller (oc8.runtime.executor) re-reads the run row on this same
        # `db` the instant execute() returns, so a commit here must leave the
        # session re-bound just as much as the mid-loop one.
        await _rebind(db, tenant_id)

        return RunResult(
            task_id=task_id,
            agent_id=agent.id,
            status="done",
            output=message,
        )


def register(contrib: Any) -> None:
    # A CLASS, not an instance: PluginContributions.add_runtime is typed
    # `type[RuntimeAdapter]` and oc8.runtime.registry.resolve_runtime does
    # `impl()` on whatever it gets back (registry.py ~line 206) -- passing
    # an instance here would make that call try to instantiate an instance,
    # which fails the moment an agent is actually dispatched onto this
    # runtime. Mirrors claude_code_runtime/runtime/runtime.py's register (and
    # nanoclaw_runtime/runtime/runtime.py's register before it), which pass their own
    # class the same way.
    contrib.add_runtime(CodexRuntime)
