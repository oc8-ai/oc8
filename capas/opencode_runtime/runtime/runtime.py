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
xdgData, "opencode")` (verified directly against packages/core/src/global.ts
and packages/opencode/src/storage/) -- i.e. OUTSIDE /workspace, so that
directory is a scratch dir saved to `RunStateStore` after every leg and
restored by the next leg's seed, or teardown destroys the very thing
`--session <id>` resumes. The whole directory is saved, so session storage
kept as JSON files and a SQLite database with its `-wal`/`-shm` files alike
come back; `bin/`, `log/` and any `node_modules/` are left out. Resume is by
the explicit `--session <id>` every event line reports. XDG_DATA_HOME is set
explicitly rather than left to $HOME's default so the scratch dir and the
CLI cannot disagree about where that is. See cli_harness.seed.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from cli_harness.mint import run_token
from cli_harness.outcome import no_terminal_event
from cli_harness.park import POLL_INTERVAL_S, is_parked
from cli_harness.seed import (
    CONFIG_SCRATCH_MB,
    CONTAINER_HOME,
    HOME_SCRATCH_MB,
    WORKSPACE,
    SeedDir,
    build_seed,
    load_home,
    load_workspace,
    sandbox_lifetime_s,
    sandbox_owner,
    save_home,
    save_workspace,
    stop_cli,
    workspace_scratch,
    workspace_seed,
)
from cli_harness.session_state import clear_session_id, get_session_id, set_session_id
from cli_harness.tail import tail_new_lines
from cli_harness.toolchain import TOOLCHAIN_NOTE
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.engine import RunResult, open_run_task
from oc8.config import get_settings
from oc8.realtime.emit import publish_run_output_delta
from oc8.runtime.workspace import run_output_sandbox
from oc8.sandbox import get_sandbox_driver
from oc8.sandbox.naming import container_name
from oc8.sandbox.reaper import RUN_LABEL, TENANT_LABEL
from oc8.sandbox.types import SandboxSpec, ScratchDir

_PLUGIN_NAME = "opencode_runtime"
_MODEL_ID = "oc8-gateway"
#: The container's HOME. `node`, not `oc8agent`: the base image is
#: node:22-bookworm-slim, which already ships a uid-1000 `node` user -- the
#: `useradd --uid 1000 oc8agent` this image used to carry failed the build
#: outright ("UID 1000 is not unique"). See claude_code_runtime/
#: Dockerfile.base, which all three images share.
_CONTAINER_HOME = CONTAINER_HOME
#: `xdgData` (the xdg-basedir package opencode's packages/core/src/global.ts
#: imports) is $XDG_DATA_HOME, falling back to $HOME/.local/share. Pinned via
#: the env below so this constant and the CLI agree by construction.
_XDG_DATA_HOME = f"{_CONTAINER_HOME}/.local/share"
#: `Global.Path.data` -- opencode's own session storage root.
_OPENCODE_DATA_DIR = f"{_XDG_DATA_HOME}/opencode"
#: The name `_OPENCODE_DATA_DIR` is saved under in RunStateStore.
_HOME_STATE = "opencode-data"
#: opencode's own config directory, where opencode.jsonc lands. Named
#: explicitly through OPENCODE_CONFIG, and deliberately NOT under
#: _OPENCODE_DATA_DIR: that directory is saved after every leg, and this file
#: carries the run's bearer token, which must not land in saved state. Every
#: leg seeds it fresh. A scratch dir of its own, because opencode also
#: writes next to its config (its plugin install).
_OPENCODE_CONFIG_DIR = f"{_CONTAINER_HOME}/.config/opencode"
_OPENCODE_CONFIG_PATH = f"{_OPENCODE_CONFIG_DIR}/opencode.jsonc"
_RUN_TIMEOUT_S = 1800.0
# Same overall-deadline requirement as codex_runtime/claude_code_runtime --
# see codex_runtime's module docstring note for the full reasoning (found
# during claude_code_runtime's own task review: an unbounded poll loop never
# resolves a genuinely wedged CLI process to "failed").


def _not_saved(rel: str) -> bool:
    """What a saved data dir leaves out: opencode's downloaded binaries,
    logs and any installed packages -- large, and rebuilt on demand."""
    parts = rel.split("/")
    return parts[0] in ("bin", "log") or "node_modules" in parts


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

        # A resume leg needs the session the previous leg saved; without it
        # `--session <id>` names a session that exists nowhere. Refuse plainly
        # rather than start over -- see claude_code_runtime.
        data_files: dict[str, bytes] = {}
        if resume_session_id is not None:
            restored = await load_home(tenant_id=tenant_id, run_id=run_id, name=_HOME_STATE)
            if restored is None:
                return RunResult(
                    task_id=task_id,
                    agent_id=agent.id,
                    status="failed",
                    output=(
                        f"opencode cannot resume session {resume_session_id}: "
                        "the run's saved data directory is missing"
                    ),
                )
            data_files = restored
        # Whatever the previous leg left in /workspace (without output/).
        workspace_files = await load_workspace(tenant_id=tenant_id, run_id=run_id) or {}
        uid, gid = sandbox_owner()

        spec = SandboxSpec(
            image=settings.opencode_agent_image,
            name=container_name(agent.name, "opencode", run_id),
            # Identity for the startup reaper; the name is only for humans.
            labels={RUN_LABEL: str(run_id), TENANT_LABEL: str(tenant_id)},
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
                # a writable scratch dir (its parents are created root-owned
                # as mount points), so an unset XDG_STATE_HOME sends opencode
                # to mkdir a sibling it cannot create. Observed live: every
                # run failed with
                # `EACCES: permission denied, mkdir '/home/node/.local/state'`
                # before opencode logged anything else, including to the CLI
                # events this plugin parses -- so the container just exited 1
                # with an empty session dir and no diagnostic anywhere oc8
                # itself could surface. Pointed inside the already-writable
                # data dir, so it is saved and restored with the session.
                "XDG_STATE_HOME": f"{_OPENCODE_DATA_DIR}/state",
            },
            workdir=WORKSPACE,
            network_disabled=False,
            network=settings.agent_runtime_network,
            egress="restricted",
            max_lifetime_s=sandbox_lifetime_s(_RUN_TIMEOUT_S),
            scratch=(
                workspace_scratch(),
                ScratchDir(_OPENCODE_DATA_DIR, HOME_SCRATCH_MB),
                ScratchDir(_OPENCODE_CONFIG_DIR, CONFIG_SCRATCH_MB),
            ),
            # opencode.jsonc in place BEFORE `opencode` starts, the same race
            # codex_runtime's seed closes: a config written after start could
            # be read too late, and opencode would run against its default
            # provider with no oc8 MCP server. On a resume leg the seed also
            # restores the saved data directory.
            seed=build_seed(
                [
                    *workspace_seed(workspace_files),
                    SeedDir(
                        _OPENCODE_CONFIG_DIR,
                        {
                            "opencode.jsonc": _opencode_config(
                                llm_url=llm_url, mcp_url=mcp_url, token=token
                            ).encode()
                        },
                    ),
                    SeedDir(_OPENCODE_DATA_DIR, data_files),
                ],
                uid=uid,
                gid=gid,
            ),
            # The seed belongs to this uid; see cli_harness.seed.sandbox_owner.
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
        exited = False
        try:
            async with run_output_sandbox(run_id, tenant_id, spec, driver, handle):
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
                                output=str(
                                    (run.context.get("isolated_result") or {}).get("output", "")
                                ),
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
                            exited = True
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
                    # Every leg, whatever its outcome, before teardown: a
                    # parked leg's next leg resumes from exactly this. On a
                    # park the CLI may still be running; SQLite's -wal file
                    # comes along, so committed transactions survive.
                    if not exited:
                        await stop_cli(driver, handle)
                    await save_home(
                        driver,
                        handle,
                        tenant_id=tenant_id,
                        run_id=run_id,
                        name=_HOME_STATE,
                        path=_OPENCODE_DATA_DIR,
                        exclude=_not_saved,
                    )
                    await save_workspace(driver, handle, tenant_id=tenant_id, run_id=run_id)
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
