"""Runs an agent's task through the opencode CLI in its own container. See
claude_code_runtime/runtime/runtime.py's module docstring for the shared design.
opencode-specific: it is multi-provider by design, so oc8's gateway is
configured as a CUSTOM provider (opencode.jsonc's provider.<id>.options.
baseURL with the @ai-sdk/openai-compatible package), not read from an env
var pair -- verified against packages/web/src/content/docs/providers.mdx's
own "Custom provider" section in the opencode repo. MCP is opencode's native
"remote" type (packages/web/src/content/docs/mcp-servers.mdx), same shape
as Claude Code's "http" and Codex's "streamable_http".

Its resumable session lives in opencode's DATA directory -- `path.join(
xdgData, "opencode")` then `storage/session/...` (verified directly against
packages/core/src/global.ts and packages/opencode/src/storage/storage.ts) --
i.e. OUTSIDE /workspace, so that directory is a per-run HOST directory
mounted read-write, or teardown destroys the very thing `--session` resumes.
XDG_DATA_HOME is set explicitly rather than left to $HOME's default so the
mount and the CLI cannot disagree about where that is.
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

_PLUGIN_NAME = "opencode_runtime"
_MODEL_ID = "oc8-gateway"
#: The container's HOME. `node`, not `oc8agent`: the base image is
#: node:22-bookworm-slim, which already ships a uid-1000 `node` user -- the
#: `useradd --uid 1000 oc8agent` this image used to carry failed the build
#: outright ("UID 1000 is not unique"). See claude_code_runtime/
#: Dockerfile.base, which all three images share.
_CONTAINER_HOME = "/home/node"
#: `xdgData` (the xdg-basedir package opencode's packages/core/src/global.ts
#: imports) is $XDG_DATA_HOME, falling back to $HOME/.local/share. Pinned via
#: the env below so this constant and the CLI agree by construction.
_XDG_DATA_HOME = f"{_CONTAINER_HOME}/.local/share"
#: `Global.Path.data` -- opencode's own session storage root.
_OPENCODE_DATA_DIR = f"{_XDG_DATA_HOME}/opencode"
#: Where opencode.jsonc lands INSIDE the container. Named explicitly through
#: OPENCODE_CONFIG, and deliberately NOT under _OPENCODE_DATA_DIR -- see
#: _write_opencode_config's docstring for why it stays independent of the
#: writable session directory.
_OPENCODE_CONFIG_PATH = f"{_CONTAINER_HOME}/.config/opencode/opencode.jsonc"
_RUN_TIMEOUT_S = 1800.0
# Same overall-deadline requirement as codex_runtime/claude_code_runtime --
# see codex_runtime's module docstring note for the full reasoning (found
# during claude_code_runtime's own task review: an unbounded poll loop never
# resolves a genuinely wedged CLI process to "failed").

#: Test-only override for the host root this run's directories are created
#: under. Mirrors codex_runtime/runtime/runtime.py's SESSION_ROOT_OVERRIDE (itself
#: mirroring nanoclaw_runtime/runtime/runtime.py's SESSION_ROOT_OVERRIDE) exactly, and for
#: the same reason: writing the config and creating the mount sources on a
#: real host path before provision() means this plugin does REAL disk I/O
#: even under a FakeDriver, and the default runtime_session_root
#: (/var/lib/oc8/sessions) is neither writable nor expected to exist outside
#: a deployed container.
SESSION_ROOT_OVERRIDE: str | None = None


def _workspace_dir(session_root: str, run_id: uuid.UUID) -> str:
    return f"{session_root}/{run_id}"


def _data_home_dir(session_root: str, run_id: uuid.UUID) -> str:
    """Backs opencode's data directory -- where `--session <id>` finds its
    session storage on the next leg.

    Per-RUN and a SIBLING of /workspace's host directory, exactly like
    claude_code_runtime's `_config_home_dir`, codex_runtime's
    `_codex_home_dir`, and nanoclaw's `claude_home_dir` before all of them: a
    resume leg is the SAME run, so per-run supplies everything the CLI's own
    resume needs, while a per-AGENT folder would let one run leave state a
    LATER, unrelated run of the same agent reads back as its own session.
    """
    return f"{session_root}/{run_id}-opencode-home"


def _config_dir(session_root: str, run_id: uuid.UUID) -> str:
    return f"{session_root}/{run_id}-opencode-config"


def _opencode_config(*, llm_url: str, mcp_url: str, token: str) -> str:
    return json.dumps(
        {
            "$schema": "https://opencode.ai/config.json",
            "provider": {
                "oc8": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "oc8",
                    "options": {"baseURL": llm_url, "apiKey": token},
                    "models": {_MODEL_ID: {"name": "oc8 gateway"}},
                }
            },
            "mcp": {
                "oc8": {
                    "type": "remote",
                    "url": mcp_url,
                    "enabled": True,
                    "headers": {"Authorization": f"Bearer {token}"},
                }
            },
        }
    )


def _write_opencode_config(
    *, session_root: str, run_id: uuid.UUID, llm_url: str, mcp_url: str, token: str
) -> str:
    """Write opencode.jsonc to a HOST path before the container starts, and
    return that path for the caller to bind-mount in -- NOT a driver.fs_write
    call into an already-running container.

    Mirrors codex_runtime's _write_config_toml fix exactly (see that
    function's docstring for the full race this closes): provision() starts
    the container -- and, via the base image's tini entrypoint, `opencode
    run ...` -- immediately, and fs_write is a SEPARATE Docker API
    round-trip with nothing synchronizing the two. If `opencode` reads its
    config before fs_write lands, it starts pointed at whatever default
    provider config ships in the image (no oc8 custom provider, no oc8 MCP
    server), which defeats exactly what this plugin's config exists to set
    up -- silently and non-deterministically, since a FakeDriver (which
    provisions instantly) would never observe the race.

    Its own host directory, and its own CONTAINER directory too: it is
    mounted read-only at `$HOME/.config/opencode/`, which is not nested
    inside the read-write data directory (`$XDG_DATA_HOME/opencode`) at all,
    so the agent's own file-edit tools never get a writable second path to
    the file carrying this run's bearer token and MCP endpoint. That is the
    same class of bypass nanoclaw_runtime/runtime/session.py's platform_mounts
    docstring documents for settings.json, closed here by construction
    rather than by a layered mount.

    Written 0600 and handed to the sandbox uid (`write_private_file`): this
    file embeds the run's bearer JWT and outlives its container on the host.
    """
    path = f"{_config_dir(session_root, run_id)}/opencode.jsonc"
    return write_private_file(path, _opencode_config(llm_url=llm_url, mcp_url=mcp_url, token=token))


def _build_command(*, task_text: str, resume_session_id: str | None) -> list[str]:
    # task_text is free-form (a human/agent task description, possibly
    # starting with "-") passed to a BARE positional with nothing before it
    # consuming it as an explicit flag value -- codex_runtime hit the same
    # shape (Task 4's review, an automated security scan) and the fix is the
    # same standard CLI convention: a "--" end-of-flags separator so the
    # argument parser cannot mistake task_text for a flag. All flags go
    # BEFORE "--"; only the positional message goes after it.
    cmd = [
        "opencode",
        "run",
        "--format",
        "json",
        "--auto",
        "--model",
        f"oc8/{_MODEL_ID}",
    ]
    if resume_session_id is not None:
        cmd += ["--session", resume_session_id]
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


def _session_id(events: list[dict[str, Any]]) -> str | None:
    # `sessionID` is a TOP-LEVEL field on every emitted line (verified
    # directly against anomalyco/opencode's packages/opencode/src/cli/cmd/
    # run.ts -- a review of this plugin found the original code here
    # invented a "session.created" event type that does not exist in
    # opencode's real --format json output), not a payload nested under a
    # dedicated "session created" event -- so the first event carrying it
    # wins, regardless of that event's `type`.
    for event in events:
        sid = event.get("sessionID")
        if sid:
            return str(sid)
    return None


def _final_message(events: list[dict[str, Any]]) -> str | None:
    # opencode's real --format json output has no "message.completed"
    # event (that type does not exist -- same review finding as
    # _session_id's docstring above). The assistant's response arrives as
    # one or more `"text"`-typed events, each carrying its segment as
    # `part.text`; by the time a "text" event reaches stdout its part is
    # already finalized (part.time.end set upstream), so every "text" event
    # observed here is a real, complete segment. A turn can emit several
    # (multiple text segments), so concatenate them in emission order rather
    # than keeping only the last one.
    texts = [
        event["part"]["text"]
        for event in events
        if event.get("type") == "text"
        and isinstance(event.get("part"), dict)
        and event["part"].get("text")
    ]
    return "\n".join(texts) if texts else None


async def _rebind(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Re-pin the RLS GUC after a commit -- it is transaction-local
    (`set_config(..., is_local=true)`, oc8.db.session.tenant_session), so
    every statement after a commit runs unbound otherwise and RLS fails
    closed: a `db.refresh(run)` right after the mid-loop commit below would
    find zero rows and raise, not silently see a stale one. Mirrors
    claude_code_runtime/runtime/runtime.py's _rebind / codex_runtime/runtime/runtime.py's _rebind
    (themselves copied from nanoclaw_runtime/runtime/runtime.py's NanoclawRuntime._rebind)
    exactly, for the same reason."""
    await db.execute(
        sql_text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
    )


class OpencodeRuntime:
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
        assert run_id is not None, "opencode_runtime requires a run row to correlate resumes"
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
        llm_url = f"{settings.internal_base_url}/llm/v1"
        mcp_url = f"{settings.internal_base_url}/mcp"
        resume_session_id = get_session_id(run, _PLUGIN_NAME)
        session_root = SESSION_ROOT_OVERRIDE or settings.runtime_session_root

        # All three mount sources are created HERE, before provision():
        # Docker auto-creates a missing bind-mount source as root:root, which
        # the container's unprivileged uid then cannot write to -- silently
        # defeating both /workspace and the resumable session. See
        # cli_harness.dirs.
        workspace_path = ensure_dir(_workspace_dir(session_root, run_id))
        data_home_path = ensure_dir(_data_home_dir(session_root, run_id))
        config_path = _write_opencode_config(
            session_root=session_root,
            run_id=run_id,
            llm_url=llm_url,
            mcp_url=mcp_url,
            token=token,
        )

        spec = SandboxSpec(
            image=settings.opencode_agent_image,
            name=container_name(agent.name, "opencode", run_id),
            # Identity for the startup reaper; the name is only for humans.
            labels={RUN_LABEL: str(run_id)},
            command=_build_command(
                task_text=f"{task_text}\n\n{TOOLCHAIN_NOTE}", resume_session_id=resume_session_id
            ),
            env={
                "OPENCODE_CONFIG": _OPENCODE_CONFIG_PATH,
                # Both explicit because SandboxSpec.user pins a raw HOST uid
                # the image's passwd db does not know, so Docker would
                # otherwise default HOME to "/" -- and opencode's data
                # directory is derived from exactly these two.
                "HOME": _CONTAINER_HOME,
                "XDG_DATA_HOME": _XDG_DATA_HOME,
                # opencode's xdg-basedir package resolves a SEPARATE state
                # dir from XDG_STATE_HOME (default $HOME/.local/state), not
                # from XDG_DATA_HOME -- and only _OPENCODE_DATA_DIR itself is
                # bind-mounted writable, so an unset XDG_STATE_HOME sends
                # opencode to mkdir a sibling of that mount on the image's
                # read-only layer. Observed live: every run failed with
                # `EACCES: permission denied, mkdir '/home/node/.local/state'`
                # before opencode logged anything else, including to the CLI
                # events this plugin parses -- so the container just exited 1
                # with an empty session dir and no diagnostic anywhere oc8
                # itself could surface. Pointed inside the already-writable
                # data mount rather than adding a fourth bind mount for it.
                "XDG_STATE_HOME": f"{_OPENCODE_DATA_DIR}/state",
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
                    # opencode's data directory writable, so `--session <id>`
                    # on the next leg can still find the session storage this
                    # leg wrote.
                    BindMount(
                        host_path=data_home_path,
                        container_path=_OPENCODE_DATA_DIR,
                        readonly=False,
                    ),
                    # ...and the config read-only, at a container path that is
                    # NOT nested inside that writable directory. Written to
                    # disk BEFORE provision() -- see _write_opencode_config.
                    BindMount(
                        host_path=config_path,
                        container_path=_OPENCODE_CONFIG_PATH,
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
        # the final event line, or -- worse -- against driver.logs() called a
        # second time after driver.teardown() has already run, which a
        # FakeDriver returning unchanging content would report as ZERO new
        # lines via the overlap-diffing tail helper) would misreport a
        # finished, successful run as failed.
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
                sid = _session_id(new_events)
                # `sessionID` rides on EVERY emitted line, so without this
                # guard the id would be re-derived and re-committed on every
                # poll batch. Committing once, like claude_code_runtime's
                # system/init and codex_runtime's thread.started, is the
                # behaviour all three now share.
                if sid is not None and sid != resume_session_id:
                    # merge_context, NOT `run.context = ...` -- see
                    # claude_code_runtime/runtime/runtime.py's identical comment and
                    # cli_harness.session_state: a whole-object write from
                    # this session's older snapshot erases the park marker the
                    # /mcp gateway just committed from its own session.
                    await set_session_id(db, run, _PLUGIN_NAME, sid)
                    await db.commit()
                    await _rebind(db, tenant_id)
                    resume_session_id = sid

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
                # marks messages delivered, and a headless `opencode run` has
                # no channel to inject them into once started, so draining
                # them would destroy operator steering rather than deliver it.

                if time.monotonic() - started_at > _RUN_TIMEOUT_S:
                    return RunResult(
                        task_id=task_id,
                        agent_id=agent.id,
                        status="failed",
                        output=f"opencode timed out after {_RUN_TIMEOUT_S:.0f}s",
                    )

                try:
                    exit_code = await driver.wait(handle, timeout_s=POLL_INTERVAL_S)
                    # One last tail BEFORE teardown, not after: each loop
                    # iteration tails logs THEN blocks in driver.wait() for
                    # up to POLL_INTERVAL_S, so a task that finishes within a
                    # single interval has its final `text` event written
                    # to stdout WHILE this call is blocked inside wait() --
                    # after the tail that already ran this iteration, before
                    # any tail that would ever run again. The container is
                    # still present here (teardown is the very next line, in
                    # the finally below), so this is the safe side of the
                    # "second driver.logs() call after teardown()" bug the
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
                output=no_terminal_event("opencode"),
            )

        if exit_code != 0 or message is None:
            return RunResult(
                task_id=task_id,
                agent_id=agent.id,
                status="failed",
                output=message or f"opencode exited {exit_code}",
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
    # runtime. Mirrors claude_code_runtime/runtime/runtime.py's register and
    # codex_runtime/runtime/runtime.py's register (and the one in
    # nanoclaw_runtime/runtime/runtime.py before them), which pass their own
    # class the same way.
    contrib.add_runtime(OpencodeRuntime)
