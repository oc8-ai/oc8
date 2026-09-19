"""Isolated agent runtime (§8.1/§8.5): each run executes in its own Docker
container — a thin, credential-free execution shell that drives the run over the
control-plane internal API. Provider keys, MCP credentials and the secret-store
KEK never enter the container; the container gets only a run-scoped token and the
internal URL. This is the local-dev (Docker) form of the one-container-per-agent
model; gVisor/K8s trust tiers and NetworkPolicy come later.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.engine import CancelCheck, InboxCheck, RunResult, _max_steps, open_run_task
from oc8.auth import get_identity_provider
from oc8.config import get_settings
from oc8.metering import check_budget, trigger_budget_hard_stop
from oc8.observability import record_budget_exceeded
from oc8.realtime.emit import record_activity
from oc8.runtime.workspace import ensure_workspace_dir, workspace_root
from oc8.sandbox import get_sandbox_driver
from oc8.sandbox.mounts import validate_mounts
from oc8.sandbox.naming import container_name
from oc8.sandbox.reaper import RUN_LABEL
from oc8.sandbox.types import BindMount, SandboxError, SandboxHandle, SandboxSpec

logger = logging.getLogger(__name__)

# The shell's verdict -> the state its task lands in. Anything unknown is a
# failure, the same way the executor treats an unknown run status.
_TASK_STATE = {
    "done": "done",
    "failed": "failed",
    "waiting_for_approval": "waiting_for_approval",
    "waiting_for_input": "waiting_for_input",
}


class DockerIsolatedRuntime:
    """Runs the agent loop inside a per-run container. Registered as
    ``oc8.agent-runtime-isolated``; an agent selects it via its runtime_ref, or a
    deployment routes every run through it with OC8_AGENT_ISOLATION=true."""

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
        cancel_check: CancelCheck | None = None,
        inbox_check: InboxCheck | None = None,
        pre_decided: dict[str, str] | None = None,
        originating_operator: str | None = None,
        # Accepted only to satisfy the RuntimeAdapter Protocol -- unused here
        # because the container never receives images through this call. It
        # reads them the same way it reads everything else about the run: off
        # `run_row.context["task_images"]`, which this method's own `ctx = dict(
        # run_row.context)` below already carries through untouched (only
        # `ctx["task"]` is overwritten). See internal_agent.py's `step()` for
        # where the container-side fetch/gate actually happens.
        task_images_raw: list[dict[str, str]] | None = None,
    ) -> RunResult:
        if run_id is None:
            raise SandboxError("isolated runtime requires a run id")

        # The container drives the run over the internal API and never creates
        # domain rows itself, so the control plane opens the Task here -- exactly
        # as the in-process engine does. Without it the run carries no task_id,
        # the approval the internal API raises carries none either, and
        # resolve_tool_approval can then never find the run to resume: an
        # approved action would strand the run in waiting_for_approval forever.
        run_row = await db.get(m.AgentRun, run_id)
        task = await open_run_task(
            db,
            agent=agent,
            task_text=task_text,
            tenant_id=tenant_id,
            parent_task_id=parent_task_id,
            delegation_depth=delegation_depth,
            # A resume leg continues the suspended leg's task; see open_run_task.
            resume_task_id=run_row.task_id if run_row is not None else None,
        )
        task_id = task.id

        # Budget gate (§15.4), the same one the in-process engine applies right
        # after opening the task -- and applied BEFORE a container is provisioned,
        # so a breaching tenant cannot spend on compute either. Returning here
        # leaves the session uncommitted on purpose: the executor commits the
        # freeze, the incident and the terminal transition together, exactly as it
        # does for an in-process run. (The mid-flight commit further down exists
        # only to unlock the run row for a container that is actually going to run.)
        budget = await check_budget(db, tenant_id=tenant_id, department_id=agent.department_id)
        if budget.hard_exceeded:
            record_budget_exceeded("hard")
            task.state = "budget_exceeded"
            await trigger_budget_hard_stop(db, tenant_id=tenant_id, breaching_agent=agent)
            await record_activity(
                db,
                tenant_id=tenant_id,
                agent_id=agent.id,
                status="warning",
                message=f"{agent.name} paused: token budget exceeded",
            )
            return RunResult(task_id, agent.id, "budget_exceeded", "Token budget exceeded.", [], 0)
        if budget.soft_exceeded:
            record_budget_exceeded("soft")
            await record_activity(
                db,
                tenant_id=tenant_id,
                agent_id=agent.id,
                status="warning",
                message=f"{agent.name} is approaching its token budget",
            )

        if run_row is not None:
            run_row.task_id = task_id
            ctx = dict(run_row.context)
            # The container reads its instruction from the run context, so the
            # task_text the executor handed us has to land there -- on a resume
            # leg that text is the approval's focused resume instruction, not the
            # original task.
            ctx["task"] = task_text
            if pre_decided:
                # A resume leg starts a fresh conversation, mirroring the
                # in-process engine (which simply runs again with the resume
                # instruction). Keeping the old transcript would replay a
                # dangling assistant tool_call -- the suspended call never got a
                # tool result -- which providers reject.
                ctx["transcript"] = []
                ctx["steps"] = 0
                ctx.pop("isolated_result", None)
            run_row.context = ctx

        # Release the executor's lock on the agent_run row BEFORE the container
        # runs: the internal API updates that row (transcript, result) from its
        # own sessions, and would otherwise block on the executor's uncommitted
        # RUNNING transition for the whole run -- a deadlock (executor waits for
        # the container, the container waits for the row lock). Committing here
        # also expires the executor's `run`, so its post-run transition reloads
        # the fresh context the container wrote.
        await db.commit()

        settings = get_settings()
        # A short-lived token scoped to EXACTLY this run. The container can call
        # the internal API only for this run, nothing else.
        token = get_identity_provider().mint(
            tenant_id=tenant_id,
            subject=f"agent:{agent.id}",
            role="agent_default",
            kind="agent",
            scopes=[f"run:{run_id}"],
        )
        # Created BEFORE provision(): Docker auto-creates a missing bind-mount
        # source as root:root, which the container's unprivileged uid then
        # cannot write to -- see the three runtime plugins' own workspace_dir
        # setup (e.g. opencode_runtime/runtime/runtime.py) for the same
        # reasoning and the same failure this avoids.
        session_root = settings.runtime_session_root
        workspace_path = ensure_workspace_dir(workspace_root(run_id))

        spec = SandboxSpec(
            image=settings.agent_runtime_image,
            name=container_name(agent.name, "shell", run_id),
            # Identity for the startup reaper; the name is only for humans.
            labels={RUN_LABEL: str(run_id)},
            # ONLY these three vars — no DB URL, no provider keys, no KEK.
            env={
                "OC8_INTERNAL_URL": settings.internal_base_url,
                "OC8_AGENT_TOKEN": token,
                "OC8_RUN_ID": str(run_id),
            },
            workdir="/app",
            command=["/opt/venv/bin/python", "-m", "oc8.isolated_shell"],
            network=settings.agent_runtime_network,
            network_disabled=False,  # must reach the control-plane internal API
            mem_limit="512m",
            pids_limit=256,
            cpu_limit=1.0,
            # First mount this runtime has ever had -- see the design's
            # Runtime Integration section: isolated-shell had no filesystem
            # sandbox at all before this, so an agent here could only hand
            # back a produced file through write_output_file. Now it behaves
            # like the other containerized runtimes: anything written under
            # /workspace/output/ is synced to FileAttachment when the run
            # ends (oc8.runtime.workspace.sync_run_output).
            mounts=validate_mounts(
                [BindMount(host_path=workspace_path, container_path="/workspace", readonly=False)],
                allowed_root=session_root,
            ),
            # Bind mount carries HOST ownership -- the container has to run as
            # the uid that created it, same as every other containerized
            # runtime plugin (see e.g. opencode_runtime's identical `user=`).
            user=settings.sandbox_user or None,
        )
        driver = get_sandbox_driver()
        handle: SandboxHandle | None = None
        try:
            handle = await driver.provision(spec)
            logger.info(
                "run %s: isolated container %s started", run_id, handle.container_id[:12]
            )
            # The bound on the WORK, and the only thing that ends a wedged
            # container: one that never exits ends here and is torn down by the
            # finally below, whether wait raises (docker-py's ReadTimeout on the
            # request timeout) or returns non-zero. Nothing else catches it --
            # while this waits the run keeps beating, and rightly so: it IS being
            # executed, and since 2026-08-02 the worker keeps its queue claim for
            # the same reason. The run ROW is then closed by the reconciler /
            # the reclaim one ABANDONED_AFTER later.
            # Note the size: _max_steps(agent) * 60 is a minute a step (this
            # agent's own override if it has one, else settings.agent_max_steps),
            # while a single step's model call alone is allowed 180s. It is a cap
            # on how long an agent may work, not a liveness guess -- must track
            # the SAME per-agent budget internal_agent.py's /step endpoint
            # enforces, or a raised per-agent override would still get killed by
            # a wall-clock timeout sized for the framework default.
            code = await driver.wait(handle, timeout_s=float(_max_steps(agent) * 60))
            if code != 0:
                logs = await driver.logs(handle)
                logger.warning(
                    "run %s: isolated container exited %s\n%s", run_id, code, logs[-800:]
                )
        finally:
            if handle is not None:
                await driver.teardown(handle)
            # The mid-flight commit above ended the transaction that carried the
            # transaction-local tenant GUC (tenant_session pins it with
            # set_config('app.tenant_id', ..., is_local=true)). Without re-binding
            # it, every subsequent statement on this session runs unbound, and RLS
            # on agent_run casts the empty setting to `""` and aborts with
            # `invalid input syntax for type uuid`. In the `finally` (not after
            # the `try`) on purpose: a `provision`/`wait` failure (e.g. a flaky
            # provisioner request) propagates out of this block as an exception,
            # and the executor's own handler catches it and immediately writes
            # `{"error": ...}` onto THIS SAME session -- unbound, that write
            # itself crashed with the UUID error above, so the real error was
            # never recorded and the run sat in "running" for the full
            # reconciler window instead of failing with a clear message.
            # Rebinding here, before the exception leaves this function, covers
            # both the success path and every failure path in one place.
            await db.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(tenant_id)},
            )

        fresh = await db.get(m.AgentRun, run_id)
        if fresh is not None:
            # The container wrote the verdict in its OWN transaction; refresh this
            # session's cached copy so we read the committed context. Refresh (an
            # async reload) rather than expire_all(): the latter would also expire
            # the executor's live `agent`, whose next attribute read (in
            # publish_agent_status) would then lazy-load in a sync context and
            # raise MissingGreenlet.
            await db.refresh(fresh)
        result = (fresh.context.get("isolated_result") if fresh is not None else None) or {}
        status = str(result.get("status", "failed"))
        # Carry the verdict onto the task too, so the office view and any
        # delegation waiting on this task see the same outcome the run does.
        task_row = await db.get(m.Task, task_id)
        if task_row is not None:
            task_row.state = _TASK_STATE.get(status, "failed")
        # Sub-runs the container delegated (§7). /tool cannot publish them -- it
        # only records them -- so they must ride out on the RunResult for the
        # executor to publish after it commits. Dropping them here would create a
        # queued run nothing ever picks up: a silently lost delegation.
        pending_raw = (fresh.context.get("pending_runs", []) if fresh is not None else []) or []
        # Same reasoning as pending_raw above: /tool (internal_agent.py) already
        # wrote every render_component call onto run.context as it happened --
        # this is the one place that copy makes it onto the RunResult the
        # executor's merge_context() persists. Omitting it here previously
        # meant the RunResult's empty default silently overwrote the real,
        # already-durable list with `[]` at the run's own terminal commit.
        rendered_components = (
            fresh.context.get("rendered_components", []) if fresh is not None else []
        ) or []
        # Same reasoning as rendered_components above: /tool already wrote every
        # todo_write call onto run.context (whole-list-replace, see internal_agent
        # .py's `ctx["todos"] = control.todos`) -- this is the one place that
        # copy makes it onto the RunResult the executor's merge_context() persists.
        todos = (fresh.context.get("todos", []) if fresh is not None else []) or []
        return RunResult(
            task_id=task_id,
            agent_id=agent.id,
            status=status,
            output=str(result.get("output", "")),
            steps=int((fresh.context.get("steps", 0)) if fresh is not None else 0),
            pending_runs=[uuid.UUID(str(r)) for r in pending_raw],
            rendered_components=list(rendered_components),
            todos=list(todos),
        )
