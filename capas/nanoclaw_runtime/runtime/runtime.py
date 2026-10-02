"""oc8 as nanoclaw's host: one run, one container (§8.7).

The harness drives its own loop. Its only way out of the container is oc8 -- the
model through /llm, every tool through /mcp -- so the PEP still sees every action
and no credential ever enters the container. That is the entire point; if this
adapter ever passes a provider key or an MCP credential, the >3000 EUR approval
stops being enforceable.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.engine import CancelCheck, InboxCheck, RunResult, open_run_task
from oc8.agent.harness.caps import resolve_caps
from oc8.agent.preamble import roster_block, system_prompt
from oc8.auth import get_identity_provider
from oc8.config import get_settings
from oc8.realtime.emit import record_activity
from oc8.runtime.approval_resume import resume_instruction
from oc8.sandbox import get_sandbox_driver
from oc8.sandbox.driver import SandboxDriver
from oc8.sandbox.mounts import validate_mounts
from oc8.sandbox.naming import container_name
from oc8.sandbox.reaper import RUN_LABEL
from oc8.sandbox.types import SandboxError, SandboxHandle, SandboxSpec
from oc8.skills.runtime import catalog_block, load_assigned_skills

from .messages import AgentMessage, to_agent_message
from .session import (
    MCP_TOOL_PREFIX,
    agent_image,
    bridge_src,
    claude_home_dir,
    group_dir,
    platform_mounts,
    provision_session,
    provisioner_image,
    runner_src,
    session_dir,
    session_mounts_for,
    skills_src,
    write_claude_md,
    write_claude_settings,
    write_container_json,
    write_routing,
)
from .session_db import (
    CHANNEL_TYPE,
    ROUTE_NAME,
    ack_status,
    heartbeat_age_s,
    read_messages_out,
    write_message_in,
)

#: NEVER `__name__`. This package is called `runtime/` (the folder convention
#: for every `runtime_adapter` plugin), so `__name__` here is `runtime.runtime`
#: -- a name claude_code_runtime, codex_runtime and opencode_runtime would all
#: share the moment any of them grows a logger, collapsing four plugins onto
#: one logger namespace and destroying per-plugin attribution in production
#: logs (spec §5.2). The pinned name keeps the plugin identity that the folder
#: name no longer carries.
logger = logging.getLogger("oc8.plugin.nanoclaw_runtime.runtime")

#: Tests point this at a tmp dir; production leaves it None and uses the setting.
SESSION_ROOT_OVERRIDE: str | None = None

_POLL_SECONDS = 2.0
#: A container that stops touching .heartbeat is stuck. Tearing it down early
#: beats waiting out the wall clock while an operator watches a dead run.
_HEARTBEAT_STALE_S = 120.0
#: A container that NEVER touched .heartbeat never came up. The stale watchdog
#: cannot see it (there is no timestamp to age), and a process that is alive but
#: silent never trips the exit watch either -- so without its own bound this case
#: burns the whole deadline and then reports a misleading timeout. Generous
#: enough for an image start plus a first turn.
_STARTUP_GRACE_S = 300.0
#: `SandboxDriver.wait` returns -1 for a docker-API fault -- container gone,
#: daemon error -- which says nothing about the process, so -1 is treated as "no
#: verdict", never as a dead container. It does NOT mean a timeout: reaching
#: `timeout_s` raises out of the awaited task instead (see the protocol's own
#: docstring, corrected 2026-08-02; this comment said the two were
#: indistinguishable, and they never were).
_NO_EXIT_VERDICT = -1
#: oc8's shared preamble ends with "reply with a short plain-text summary" --
#: correct in-process, fatal here. This harness only DELIVERS text wrapped in a
#: `<message to="...">` block; anything else is logged as scratchpad and dropped,
#: and the batch is still acked `completed`. A plain-text answer therefore
#: becomes a run that finishes with no output at all (verified live: the model
#: read Odoo, answered "Es gibt aktuell 17 Leads", and the harness logged
#: "no <message to=...> blocks -- nothing was sent"). The harness states the rule
#: itself in its system addendum, but the two instructions contradict each other
#: and the model followed ours -- so ours has to agree, and has to come LAST.
#: `operator` is the destination name `write_routing` creates, so the two must
#: keep saying the same word.
_READINESS_BLOCK = (
    # The bridge to oc8 is a subprocess the harness starts alongside the model,
    # and the model gets its first turn before that handshake finishes. Its tools
    # then answer "No such tool available: mcp__oc8__… The MCP server \'oc8\' is
    # still starting" -- from which a model reasonably concludes it has NO tools
    # and gives up, reporting a configuration problem that does not exist.
    # Observed live 2026-07-30, repeatedly; one run happened to discover
    # `WaitForMcpServers` by itself, recovered, and did its job normally.
    #
    # A rule rather than a retry loop in code because the wait belongs to the
    # harness, which owns the tool. One sentence turns a dead run into a normal
    # one.
    "Before your first tool call: the oc8 tools (`mcp__oc8__…`) may still be "
    "starting, and until they are ready they answer `No such tool available`. "
    "That is not a fault and not a missing configuration -- call "
    "`WaitForMcpServers` once, then use them. Never conclude from that message "
    "that you have no tools."
)


#: A model that needs something from a human WILL reach for a way to ask, and in
#: this runtime every obvious way is wrong. It is TOLD to reach for the wrong one:
#: tenants write `ask_user` into their agents' standing instructions ("Fehlt dir
#: eine Information …, frag per `ask_user` nach" appears in every retained
#: CLAUDE.md under `~/oc8-sessions/sessions/**`), and oc8's own `ask_user`
#: (`oc8.agent.control_tools`) is deliberately NOT advertised over the tool
#: gateway, because it suspends the RUN and MCP has no vocabulary for that (see
#: `api/mcp_gateway.py`, which refuses every `CONTROL_TOOL_NAMES` member by
#: name). Bare tool names miss the same way in the retained transcripts --
#: `No such tool available: search_records`, `: send_message` -- so this is the
#: general shape of the failure, not a one-off. Then, on 2026-08-02, one model
#: found the harness's own `mcp__nanoclaw__ask_user_question`, which blocks the
#: container on a channel oc8 does not serve, and the run died.
#:
#: (An earlier version of this comment counted "nine calls to a bare `ask_user`,
#: one to `mcp__oc8__ask_user`". That number is not reproducible: the retained
#: sessions contain zero `ask_user` calls -- the only hits are the tenant
#: instruction quoted above -- and the session that DID wedge has since been
#: reaped by evidence retention. Counts here have to be re-checkable.)
#:
#: `session.DENIED_HARNESS_TOOLS` now takes that last door away, and taking a
#: door away without showing the open one is the worse bug: an agent that cannot
#: ask at all guesses instead. `request_decision` is the open one -- it is on the
#: gateway's core list for every agent, it hands the question to a human's inbox,
#: and it does NOT suspend the caller, which is precisely why it survives here.
_ASKING_BLOCK = (
    "If you need something only a human can give you -- a decision, a missing fact, a "
    f"go-ahead -- use `{MCP_TOOL_PREFIX}request_decision`. It is the ONLY way to reach a "
    "person from here. Do not look for `ask_user` or a question tool from the harness: "
    "`ask_user` is not available in this runtime, and the harness's question tool waits "
    "for an answer on a channel nobody is listening to, which ends your run. "
    f"`{MCP_TOOL_PREFIX}request_decision` does not pause you: you ask, you keep working "
    "with what you already know, and the answer comes back later as a new task. So say "
    "in your final message what you asked for and what you could not finish without it."
)


_DELIVERY_BLOCK = (
    "How you deliver an answer here: this runtime only sends text that is wrapped in a "
    f'`<message to="{ROUTE_NAME}">...</message>` block. That overrides any instruction '
    "above to reply with a plain-text summary. Put your final answer for the operator "
    f'inside such a block, addressed to exactly `{ROUTE_NAME}` -- text outside one, or '
    "addressed to any other name, is treated as private scratchpad and is never "
    "delivered, so it reaches nobody. Use `<internal>...</internal>` for thinking you do "
    "not want sent. Do all your tool work FIRST and send the message in a turn of its "
    "own: a turn that both calls a tool and carries the delivery block is refused by the "
    "model endpoint, and the refusal reaches the operator instead of your answer. "
    # This is a fact about the environment, not another rule for the model. The
    # harness offers a `SendMessage` tool that looks exactly like the way to
    # reach an operator and cannot be: it addresses sibling agents inside this
    # container. Worse, it says so misleadingly -- "No agent named 'operator' is
    # currently addressable. Spawn a new one or use the agent ID." reads like a
    # setup problem, so a model retries it. Observed live 2026-07-29: ten of a
    # lead's twenty-two steps spent on that tool before it gave up and wrote its
    # report as plain text.
    "One trap: the `SendMessage` tool does NOT reach the operator -- it addresses other "
    "agents inside this container, and it will tell you the operator is not "
    "addressable. That is expected, not a fault to work around. The block above is the "
    "only way out."
)
#: Appended to EVERY wake message, not only to a resumed one.
#:
#: `_DELIVERY_BLOCK` above already says this in the standing instructions, and
#: twice that was not enough: a task text that arrives with its own step-by-step
#: flow is the NEARER instruction, and the model follows it. Both times the work
#: was performed correctly and the run was recorded `failed`, because the report
#: was written as plain text and reached nobody -- first a cron task whose own
#: step 7 said only "summarise", then the follow-up run of a human decision,
#: whose instruction is written by CORE and therefore cannot mention this
#: runtime's delivery convention without leaking it into every other runtime.
#: The convention lives here, so the reminder belongs here. Appended (never
#: prepended) so it is the LAST word the model reads, same reasoning as
#: `_DELIVERY_BLOCK`'s own placement.
_DELIVERY_REMINDER = (
    f'\n\nDenk daran: Nur Text in einem `<message to="{ROUTE_NAME}">...</message>`-Block '
    "erreicht den Operator. Sende deine Zusammenfassung in genau so einem Block, nicht als "
    "einfachen Text."
)
#: How nanoclaw's own host starts the runner (verified in Task 0's spike): the
#: image's entrypoint.sh reads a stdin JSON blob that this flow never sends, so
#: the real invocation bypasses it and runs the mounted source directly.
_COMMAND = ["bash", "-c", "exec bun run /app/src/index.ts"]
#: The run's verdict -> the state its task lands in. `interrupted` is a RUN
#: state and is NOT a legal task state (task's check constraint lists none), so
#: a cancelled run's task is `failed` -- exactly what the in-process engine does.
_TASK_STATE = {
    "done": "done",
    "failed": "failed",
    "interrupted": "failed",
    # A legal task state (ck_task_state), and the office view needs it: a task
    # that reads `failed` while a human is being waited for is a lie.
    "waiting_for_approval": "waiting_for_approval",
}


#: Fingerprints of an error THIS control plane produced. The LLM gateway answers
#: a dead upstream with `<status> from <url>: <body>` (502) and its streaming
#: surfaces with an in-band `upstream_error` frame; the tool gateway prefixes a
#: refusal with ERROR. When such a string comes back as the agent's answer, the
#: harness delivered our own failure text as if the model had said it.
_GATEWAY_ERROR_MARKS = (
    "upstream_error",
    "/llm/v1/",
)

#: `http_errors._error`'s exact format, which only this control plane emits.
#: Replaces a former bare `"gateway "` mark: that one also matched a lead named
#: "Gateway Freigabe", so a run whose action had genuinely succeeded was recorded
#: as failed. It caught the real errors only incidentally, via the harness's
#: "check your inference gateway (backend:8099)" copy -- third-party wording we do
#: not control. The status-and-URL line below is in every one of those bodies.
_UPSTREAM_STATUS_LINE = re.compile(r"\b\d{3} from https?://")


def _is_gateway_error(text: str) -> bool:
    """Whether a delivered "answer" is really this control plane's error text.

    Deliberately narrow: it matches shapes oc8 itself emits, not "text that looks
    like an error". A model is allowed to write the word error.
    """
    lowered = text.lower()
    if any(mark.lower() in lowered for mark in _GATEWAY_ERROR_MARKS):
        return True
    return _UPSTREAM_STATUS_LINE.search(lowered) is not None


def _root() -> str:
    return SESSION_ROOT_OVERRIDE or get_settings().runtime_session_root


@dataclass
class _Progress:
    """What the poll loop has already seen, carried across sweeps."""

    seen_seq: int = 0
    last_text: str = ""



#: `<internal>…</internal>` is how a model marks thinking it does not want sent.
_INTERNAL = re.compile(r"<internal>.*?</internal>", re.DOTALL)


async def _work_already_done(db: AsyncSession, task_id: uuid.UUID | None) -> str | None:
    """What this task changed outside oc8, phrased for an operator, or None.

    "Nothing was said" and "nothing was done" are not the same thing, and the
    ack alone cannot tell them apart. Live, 2026-07-28: a run answered a customer
    three times and moved their ticket, then returned an empty final turn -- and
    the board said only "the harness produced no message", sending whoever read
    it to look for work that was already finished.

    Counted from `tool_invocation`, which records exactly the side-effectful
    calls (reads are deliberately not in there), and keyed on the TASK so a
    resumed leg still sees what its earlier leg did. Best-effort: this decorates
    a failure message, so it must never be able to cause one.
    """
    if task_id is None:
        return None
    try:
        count = (
            await db.execute(
                select(func.count())
                .select_from(m.ToolInvocation)
                .where(m.ToolInvocation.task_id == task_id)
            )
        ).scalar_one()
        if not count:
            return None
        task = await db.get(m.Task, task_id)
        last = (task.meta_label if task is not None else None) or ""
    except Exception:
        logger.warning("could not summarise what the run had already done", exc_info=True)
        return None
    tail = f", zuletzt: {last}" if last else ""
    # Marked as oc8's own words: an operator must be able to tell a report the
    # agent wrote from one this system reconstructed after it fell silent.
    return (
        f"[von oc8 zusammengefasst] Der Agent hat {count} Aktion(en) ausgeführt{tail}, "
        "dann aber nichts mehr gesagt. Die Arbeit ist erfolgt; es fehlt nur seine "
        "eigene Meldung darüber."
    )



def _unsent_answer(session: str) -> str | None:
    """The last thing the model actually wrote, if it never sent it, else None.

    This runtime delivers only text wrapped in a `<message to="operator">` block.
    A model that writes its report as plain text has DONE the work and lost the
    report -- and the answer to that has twice been another reminder, in the
    standing instructions and then on every wake message. Observed live
    2026-07-29 with both reminders in place: a lead read the ticket system,
    decomposed an incoming order, wrote the whole result as a final plain-text
    turn, and the run was recorded `failed` with nothing to show for it.

    So oc8 corrects the mistake instead of reprimanding it. The transcript is
    this plugin's own session folder, and the model's final turn is in it; taking
    the text from there costs one file read and turns a lost run into a reported
    one.

    Deliberately narrow:

    * only the LAST assistant turn -- earlier ones are working notes, not a
      report, and stitching them together would invent a summary nobody wrote;
    * nothing wrapped in `<internal>`, which is how this harness lets a model say
      "do not send this". Overriding that would deliver exactly what it withheld;
    * best-effort throughout -- this runs on a path that is already failing, so
      it must never be able to raise.
    """
    root = os.path.join(session, "claude-home", "projects")
    try:
        files = [
            os.path.join(parent, name)
            for parent, _, names in os.walk(root)
            for name in names
            if name.endswith(".jsonl")
        ]
        if not files:
            return None
        newest = max(files, key=os.path.getmtime)
        last: str | None = None
        with open(newest, encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                content = entry.get("message", {}).get("content")
                if not isinstance(content, list):
                    continue
                texts = [
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ]
                joined = "\n".join(t for t in texts if t.strip())
                if joined.strip():
                    last = joined
    except Exception:
        logger.warning("could not read the transcript for an unsent answer", exc_info=True)
        return None
    if last is None:
        return None
    without_internal = _INTERNAL.sub("", last).strip()
    if not without_internal:
        return None
    # Marked as oc8's own doing, for the same reason _work_already_done is: an
    # operator must be able to tell a report that was delivered from one that had
    # to be fetched out of a transcript.
    return f"[von oc8 nachgereicht] {without_internal}"


class NanoclawRuntime:
    """Runs one oc8 run inside nanoclaw's agent container."""

    #: What the harness writes into the session folder for ITS own purposes, and
    #: which nothing in oc8 ever reads back. Core's evidence sweep leaves these
    #: out of the archive and reports how many bytes it dropped -- it cannot
    #: know them itself, because a vendored harness's private layout is exactly
    #: what core must not contain (see EvidenceProducingRuntime).
    #:
    #: Measured over 406 real runs, this is 34 % of everything the runtime
    #: leaves on disk -- 96 kB per run, the single largest class of bytes,
    #: bigger than the transcript. `telemetry/` dominates it: the Claude Agent
    #: SDK spools analytics events it could not deliver, and an oc8 agent
    #: container has no route to Anthropic's endpoint, so every event ever
    #: emitted is still sitting there.
    evidence_excludes: tuple[str, ...] = (
        "claude-home/telemetry",
        "claude-home/shell-snapshots",
        "claude-home/backups",
        "claude-home/statsig",
        "claude-home/.last-cleanup",
        ".heartbeat",
    )

    def evidence_dir(self, *, agent_id: uuid.UUID, run_id: uuid.UUID) -> str | None:
        """Where this run's session folder is, if it is still there.

        The same `session_dir` the run itself was given, so the sweep archives
        what the container actually saw rather than a path reconstructed from a
        second set of rules that could drift away from it.
        """
        path = session_dir(_root(), agent_id, run_id)
        return path if os.path.isdir(path) else None

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
        # `run_row.context["task_images"]`, exactly as runtime/isolated.py's
        # DockerIsolatedRuntime.execute() does for the other container-based
        # runtime.
        task_images_raw: list[dict[str, str]] | None = None,
    ) -> RunResult:
        if run_id is None:
            raise SandboxError("the nanoclaw runtime requires a run id")

        run_row = await db.get(m.AgentRun, run_id)
        task = await open_run_task(
            db,
            agent=agent,
            task_text=task_text,
            tenant_id=tenant_id,
            parent_task_id=parent_task_id,
            delegation_depth=delegation_depth,
            resume_task_id=run_row.task_id if run_row is not None else None,
        )
        if run_row is not None:
            run_row.task_id = task.id
            if run_row.context.get("resolved_tool_approvals"):
                # A resume leg: the gateway (backend/src/oc8/api/mcp_gateway.py)
                # wrote isolated_result={"status": "waiting_for_approval", ...}
                # before it parked the PREVIOUS leg, and approval_resume.py never
                # clears it on decision. Left in place, _held_for_approval's very
                # first poll below -- before the container has had any chance to
                # boot, let alone act on the decision -- re-reads that stale
                # marker and instantly re-parks the run on the old message,
                # without ever giving the approved (or rejected) call a chance to
                # run. Live-observed 2026-07-27: an operator's approval never
                # took effect because of exactly this.
                run_row.context = {
                    k: v for k, v in run_row.context.items() if k != "isolated_result"
                }
        # Composed while the session is still bound and unlocked -- it reads the
        # agent's assigned skills, and that read is RLS-scoped by the GUC the
        # commit below drops.
        standing = await self._standing_instructions(db, agent=agent, tenant_id=tenant_id)
        # Release the executor's lock before the container runs: the gateways
        # write to this run's rows from their own sessions, and would otherwise
        # block on our uncommitted transaction for the whole run.
        await db.commit()
        await self._rebind(db, tenant_id)

        settings = get_settings()
        token = get_identity_provider().mint(
            tenant_id=tenant_id,
            subject=f"agent:{agent.id}",
            role="agent_default",
            kind="agent",
            scopes=[f"run:{run_id}"],
        )
        root = _root()
        driver = get_sandbox_driver()
        session = await provision_session(
            root,
            agent_id=agent.id,
            run_id=run_id,
            image=provisioner_image(),
            driver=driver,
            label=agent.name,
        )
        group = group_dir(root, agent.id, run_id)
        write_container_json(
            group,
            assistant_name=agent.name,
            group_name=agent.name,
            agent_id=agent.id,
            mcp_url=f"{settings.internal_base_url}/mcp",
        )
        write_claude_md(group, preamble=standing)
        # After provisioning, which writes this file too: the other order
        # would drop the deny list.
        write_claude_settings(root, agent.id, run_id)
        # Not optional: without a destination the harness drops the model's reply
        # and acks the message anyway -- success with an empty output (Task 0, Q1).
        write_routing(session, run_id=run_id)

        # The TASK is a message, not standing instruction: it is what the harness
        # wakes on, and CLAUDE.md would make this run's instruction permanent.
        #
        # A resume leg wakes the harness with the DECISION, not the task: the
        # gateway has pre-decided one exact call, so the model must reproduce
        # that call rather than re-derive it -- different arguments would be a
        # different signature, and the pre-decision would refuse them.
        entries = list(
            (run_row.context or {}).get("resolved_tool_approvals", [])
            if run_row is not None
            else []
        )
        wake_text = (
            resume_instruction(entries) if entries else task_text
        ) + _DELIVERY_REMINDER
        message_id = await asyncio.to_thread(
            write_message_in,
            session,
            text=wake_text,
            channel_type=CHANNEL_TYPE,
            platform_id=str(run_id),
        )

        spec = SandboxSpec(
            image=agent_image(),
            name=container_name(agent.name, "agent", run_id),
            # Identity for the startup reaper; the name is only for humans.
            labels={RUN_LABEL: str(run_id)},
            # The whole security claim of this runtime is this dict.
            env={
                "ANTHROPIC_BASE_URL": f"{settings.internal_base_url}/llm",
                "ANTHROPIC_AUTH_TOKEN": token,
                "OC8_MCP_URL": f"{settings.internal_base_url}/mcp",
                "OC8_TOKEN": token,
                "TZ": "Europe/Berlin",
                "HOME": "/home/node",
            },
            workdir="/workspace",
            command=_COMMAND,
            mounts=[
                # `session_mounts_for` is the one place raw tenant input (agent_id,
                # run_id) turns into a host path, so it is the one place that has
                # to pass the allowlist -- including its `group` (== group_dir)
                # and `claude-home` entries, which `platform_mounts` below
                # reuses (unvalidated a second time) only to nest three
                # READ-ONLY file mounts on top of the same, already-validated
                # directories. `platform_mounts`' other three mounts are true
                # deployment constants, outside the session root, that never
                # touch tenant input at all.
                *validate_mounts(
                    session_mounts_for(root, agent_id=agent.id, run_id=run_id),
                    allowed_root=root,
                ),
                *platform_mounts(
                    runner_src=runner_src(),
                    skills_src=skills_src(),
                    bridge_path=bridge_src(),
                    group=group,
                    claude_home=claude_home_dir(root, agent.id, run_id),
                ),
            ],
            network=settings.agent_runtime_network,
            network_disabled=False,  # must reach the control plane's gateways
            mem_limit="2g",
            pids_limit=512,
            cpu_limit=2.0,
            user=settings.sandbox_user or None,
        )
        handle = await driver.provision(spec)
        logger.info("run %s: nanoclaw container %s started", run_id, handle.container_id[:12])
        # Seeded from the run's own context: a resume leg opens the SAME session
        # folder as the leg before it, and every row up to nanoclaw_seen_seq was
        # already streamed to the operator's activity feed. Starting back at 0
        # would push all of it a second time. `run_row` is None only in theory
        # (see the resume_task_id guard above) -- executor.py never dispatches a
        # run_id without a committed AgentRun row -- but this stays as defensive
        # as that existing guard rather than asserting on it.
        seen_seq = (
            int((run_row.context or {}).get("nanoclaw_seen_seq", 0))
            if run_row is not None
            else 0
        )
        progress = _Progress(seen_seq=seen_seq)
        try:
            status, output = await self._drive(
                db,
                driver=driver,
                handle=handle,
                session=session,
                message_id=message_id,
                agent=agent,
                tenant_id=tenant_id,
                run_id=run_id,
                task_id=task.id,
                deadline_s=float(settings.agent_max_steps * 60),
                cancel_check=cancel_check,
                inbox_check=inbox_check,
                progress=progress,
            )
        finally:
            # In a finally, not on the happy path: a leaked container keeps a
            # run-scoped token alive and holds its session folder open.
            await driver.teardown(handle)

        await self._rebind(db, tenant_id)
        # What this leg has already streamed. Without it the next leg starts at
        # zero and pushes every earlier message onto the activity feed again, so
        # an operator watches the agent repeat itself after every approval.
        fresh = await db.get(m.AgentRun, run_id, populate_existing=True)
        if fresh is not None:
            fresh.context = {**fresh.context, "nanoclaw_seen_seq": progress.seen_seq}
        task_row = await db.get(m.Task, task.id)
        if task_row is not None:
            task_row.state = _TASK_STATE.get(status, "failed")
        # Sub-runs this container delegated (§7). The gateway can only RECORD
        # them: publishing there would race its own commit, and a stream entry
        # whose run row is not yet visible is a run a worker picks up and cannot
        # find. So they ride out here and the executor publishes after it
        # commits. Dropping them would queue a run nobody ever starts -- a
        # delegation that vanishes silently, which is worse than a refusal.
        pending = (fresh.context.get("pending_runs", []) if fresh is not None else []) or []
        return RunResult(
            task_id=task.id,
            agent_id=agent.id,
            status=status,
            output=output,
            steps=0,
            pending_runs=[uuid.UUID(str(r)) for r in pending],
        )

    @staticmethod
    async def _standing_instructions(
        db: AsyncSession, *, agent: m.Agent, tenant_id: uuid.UUID
    ) -> str:
        """What goes in CLAUDE.md: the agent's STANDING instructions, nothing else.

        Everything here derives from the AGENT alone -- who it is, its guardrails,
        the skills it is assigned -- never from anything run-scoped. CLAUDE.md
        lives in the group folder, which is per-RUN and mounted read-write (see
        `nanoclaw_runtime/runtime/session.py` for why it is no longer per-agent); a resumed
        run rewrites the SAME folder on each leg, and because the content here
        depends only on the agent, every leg writes identical bytes and the
        (atomic) rewrite is benign regardless.

        The task is deliberately absent -- it travels as an inbound message, which
        is what the harness wakes on, and putting it here would make one leg's
        instruction outlast that leg.

        Retrieved knowledge (memory + KB context) is deliberately NOT injected in
        this slice, which makes an agent here weaker than the same agent
        in-process. That is an honest gap rather than a quiet leak: KB retrieval
        admits restricted material based on the model locality AT THE MOMENT OF
        RETRIEVAL, and this file would carry it into a resumed leg whose
        ModelConfig has since become a cloud model -- which nothing downstream
        can catch, since the LLM gateway passes contains_restricted=False. Doing
        it right needs a locality-aware channel, re-evaluated on every leg, that
        does not exist yet. Until it does, the agent reaches knowledge through its
        tools, over the MCP gateway, where the PEP still sees every call.
        """
        skills = await load_assigned_skills(db, agent=agent, tenant_id=tenant_id)
        # A lead needs the roster for the same reason it does in-process: without
        # real agent ids `delegate_task` is offered and every call is denied.
        # Leaving it out here made a containerised lead strictly weaker than the
        # same lead in-process -- not a documented gap, just a missing block.
        roster = await roster_block(db, agent=agent) if agent.is_team_lead else None
        # Live agent row, matching this function's own "everything here derives
        # from the AGENT alone" contract above -- this plugin has never taken a
        # pinned run version, so caps/tenant_name are resolved the same way.
        model_config = (
            await db.get(m.ModelConfig, agent.model_config_id) if agent.model_config_id else None
        )
        caps = resolve_caps(model_config.params if model_config is not None else None)
        org = await db.get(m.Organization, tenant_id)
        tenant_name = org.name if org is not None else "the organization"
        blocks = [
            system_prompt(agent, caps=caps, tenant_name=tenant_name),
            # Prefixed: through the bridge the model sees these as
            # `mcp__oc8__skill_x`, and a catalogue naming the bare form sends it
            # hunting for a tool that is not on its list.
            catalog_block(skills, prefix=MCP_TOOL_PREFIX),
            roster,
            _READINESS_BLOCK,
            _ASKING_BLOCK,
            _DELIVERY_BLOCK,
        ]
        return "\n\n".join(block for block in blocks if block)

    async def _drive(
        self,
        db: AsyncSession,
        *,
        driver: SandboxDriver,
        handle: SandboxHandle,
        session: str,
        message_id: str,
        agent: m.Agent,
        tenant_id: uuid.UUID,
        run_id: uuid.UUID,
        task_id: uuid.UUID | None,
        deadline_s: float,
        cancel_check: CancelCheck | None,
        inbox_check: InboxCheck | None,
        progress: _Progress,
    ) -> tuple[str, str]:
        """Poll the session while the harness works. Returns (status, output)."""
        # Watched alongside the session files, because a container that dies on
        # startup (missing image, bad command) never writes a heartbeat -- the
        # stale-heartbeat watchdog only ever fires on a heartbeat that once
        # existed. Without this, a dead container hangs the run for the whole
        # deadline while an operator watches nothing happen.
        exited: asyncio.Task[int] | None = asyncio.ensure_future(
            driver.wait(handle, timeout_s=deadline_s)
        )
        started_at = time.monotonic()
        # Wall clock too: the heartbeat check compares against a FILE MTIME, and
        # a monotonic clock has no common origin with one. See heartbeat_age_s.
        started_wall = time.time()
        ends_at = started_at + deadline_s
        try:
            while True:
                if cancel_check is not None and await cancel_check():
                    return "interrupted", progress.last_text

                # Operator steering (§7.2) becomes another inbound row, so a live
                # nanoclaw agent is steered exactly like an in-process one.
                if inbox_check is not None:
                    for body in await inbox_check():
                        await asyncio.to_thread(
                            write_message_in,
                            session,
                            text=body,
                            channel_type=CHANNEL_TYPE,
                            platform_id=str(run_id),
                        )

                verdict = await self._sweep(
                    db,
                    session=session,
                    message_id=message_id,
                    agent=agent,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    progress=progress,
                )
                if verdict is not None:
                    if verdict[0] != "done":
                        # The container is still alive here and its log is the
                        # only account of WHY it finished badly -- the session DB
                        # holds what it said, never what went wrong on the way.
                        # Without this, the "acked but said nothing" case is
                        # undiagnosable after the fact, which cost a live session.
                        await self._log_container(driver, handle)
                    return verdict

                held = await self._held_for_approval(db, run_id=run_id, tenant_id=tenant_id)
                if held is not None:
                    # The gateway parked this run in its own transaction. Keeping
                    # the container alive would put the decision on a clock no
                    # human can beat: it stops touching .heartbeat while it waits,
                    # and the watchdog then kills it as unresponsive. Tearing it
                    # down is what makes the approval survivable -- the session
                    # folder stays, so the resume leg picks the conversation up.
                    logger.info("run %s parked for approval: %s", run_id, held)
                    # Best-effort, like every other line on this feed: a failed
                    # write here must not turn a legitimate park into a failed
                    # run -- the operator still sees the run's real state either
                    # way, just not this one line about it.
                    await self._record(
                        db, tenant_id=tenant_id, agent=agent,
                        message=AgentMessage(text=f"Wartet auf Freigabe: {held}", status="warning"),
                    )
                    return "waiting_for_approval", held

                if exited is not None and exited.done():
                    code = await exited
                    exited = None
                    if code != _NO_EXIT_VERDICT:
                        # The process is gone, so nothing more will ever be
                        # written -- but it may have written its answer between
                        # the sweep above and this observation. Read once more
                        # before calling it dead.
                        verdict = await self._sweep(
                            db,
                            session=session,
                            message_id=message_id,
                            agent=agent,
                            tenant_id=tenant_id,
                            task_id=task_id,
                            progress=progress,
                        )
                        if verdict is not None:
                            return verdict
                        logs = await driver.logs(handle)
                        logger.warning(
                            "nanoclaw container %s exited %s before answering\n%s",
                            handle.container_id[:12], code, logs[-800:],
                        )
                        return "failed", f"the agent container exited ({code}) before answering"
                    logger.warning(
                        "could not observe the exit of nanoclaw container %s; "
                        "falling back to the heartbeat watchdog",
                        handle.container_id[:12],
                    )

                age = await asyncio.to_thread(heartbeat_age_s, session, since=started_wall)
                if age is None:
                    # No heartbeat has EVER appeared: the container is not up.
                    if time.monotonic() - started_at > _STARTUP_GRACE_S:
                        # Same reasoning as the _sweep()-verdict branch above: the
                        # container is still alive (or was, moments ago) and its
                        # log is the only account of why -- these three watchdog
                        # exits used to skip it, which is exactly what left a live
                        # "stopped responding" run undiagnosable (Task 9c).
                        await self._log_container(driver, handle)
                        return "failed", (
                            "the agent container never came up: no heartbeat within "
                            f"{int(_STARTUP_GRACE_S)}s"
                        )
                elif age > _HEARTBEAT_STALE_S:
                    await self._log_container(driver, handle)
                    return "failed", "the agent container stopped responding"

                if time.monotonic() >= ends_at:
                    await self._log_container(driver, handle)
                    return "failed", progress.last_text or "the run exceeded its time limit"
                await asyncio.sleep(_POLL_SECONDS)
        finally:
            if exited is not None:
                exited.cancel()

    @staticmethod
    async def _log_container(driver: SandboxDriver, handle: SandboxHandle) -> None:
        """Best-effort: the container's own account of a bad ending.

        Never lets a logging failure change the run's verdict -- the verdict is
        already decided by the time this is called.
        """
        try:
            logs = await driver.logs(handle)
        except Exception as exc:  # diagnostics must never decide a run's outcome
            logger.warning("could not read nanoclaw container logs: %s", exc)
            return
        logger.warning(
            "nanoclaw container %s finished badly; its last output:\n%s",
            handle.container_id[:12], logs[-2000:],
        )

    async def _held_for_approval(
        self, db: AsyncSession, *, run_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> str | None:
        """The gateway's own marker, or None.

        Written by ANOTHER session (the tool gateway's request), so the row this
        session cached at run start says nothing -- it has to be re-read with
        populate_existing. The RLS GUC is re-bound first for the same reason it
        is after every commit here: this session has committed since it was set.
        """
        await self._rebind(db, tenant_id)
        run = await db.get(m.AgentRun, run_id, populate_existing=True)
        if run is None:
            return None
        result = run.context.get("isolated_result") or {}
        if result.get("status") != "waiting_for_approval":
            return None
        return str(result.get("output") or "an action needs human approval")

    async def _sweep(
        self,
        db: AsyncSession,
        *,
        session: str,
        message_id: str,
        agent: m.Agent,
        tenant_id: uuid.UUID,
        task_id: uuid.UUID | None,
        progress: _Progress,
    ) -> tuple[str, str] | None:
        """One read of the session: stream new messages, then check the ack.

        The SQLite calls go to a thread: they are blocking, they run every
        _POLL_SECONDS for every live run, and a worker drives several runs on one
        event loop -- so left inline they would stall each other (and every other
        coroutine in the process) on file locks held by a container that is
        writing at that moment.
        """
        rows = await asyncio.to_thread(read_messages_out, session, after_seq=progress.seen_seq)
        for row in rows:
            progress.seen_seq = row.seq
            message = to_agent_message(row)
            if message is None:
                continue
            progress.last_text = message.text
            await self._record(db, tenant_id=tenant_id, agent=agent, message=message)

        ack = await asyncio.to_thread(ack_status, session, message_id)
        if ack == "completed":
            if not progress.last_text:
                # "Completed with nothing said" has two causes that look
                # identical from here, and the difference decides the verdict.
                #
                # Nothing recorded -> broken routing, the signature Task 0 saw.
                # Reporting that as done would hand an operator an empty
                # success, so it stays FAILED.
                #
                # Side effects recorded -> the work happened and only the
                # closing sentence is missing. This used to be FAILED too, on
                # the grounds that a run which cannot report is defective. That
                # reasoning is now outweighed by a consequence that did not
                # exist when it was written: an operator who sees `failed` on a
                # ticket run re-runs it, a re-run opens a NEW task, and the
                # one-message-per-recipient guard is scoped to a task -- so the
                # customer gets a second answer. Calling finished work "failed"
                # is not merely untidy, it invites the exact duplicate the guard
                # exists to prevent. oc8 supplies the missing report from what
                # it recorded, and says that it did.
                logger.warning("run: harness completed without any deliverable message")
                did = await _work_already_done(db, task_id)
                if did is not None:
                    return "done", did
                # Nothing was recorded outside oc8 -- but a run can also do all
                # its work in ONE final turn (decompose an order, decide, plan)
                # and lose it purely to the delivery convention. Read before it
                # is called a failure.
                unsent = _unsent_answer(session)
                if unsent is not None and not _is_gateway_error(unsent):
                    logger.warning("run: recovered an undelivered answer from the transcript")
                    return "done", unsent
                return "failed", "the harness produced no message"
            if _is_gateway_error(progress.last_text):
                # The harness has its own last-resort delivery: when a turn ends
                # in an error with no <message> envelope, it sends the raw error
                # text as if it were the answer, and still acks `completed`.
                # Observed live: ten retries against a provider 400, then oc8's
                # own 502 text handed to the operator as Nora's reply, run marked
                # done. We can recognise it because the text is OUR error format,
                # produced by this control plane -- so this is not text
                # classification, it is spotting our own fingerprint.
                logger.warning("run: the harness delivered a gateway error as its answer")
                return "failed", progress.last_text
            return "done", progress.last_text
        if ack == "failed":
            return "failed", progress.last_text or "the harness reported a failure"
        return None

    async def _record(
        self, db: AsyncSession, *, tenant_id: uuid.UUID, agent: m.Agent, message: AgentMessage
    ) -> None:
        """Put one agent message on the activity feed, best-effort.

        The feed is a VIEW of the run, not the run: a failed write here must not
        throw away work the container really did.

        The SAVEPOINT is what makes that true. Catching the error and calling
        `db.rollback()` would not: a session-wide rollback expires every object
        in the identity map, and the next attribute read on one of them (the run's
        Task, on the way out) lazy-loads in a sync context and raises
        MissingGreenlet -- so guarding the run against a broken activity feed
        would itself kill the run. A savepoint rolls back only this write and
        leaves the surrounding transaction, and its objects, untouched.
        """
        try:
            async with db.begin_nested():
                await record_activity(
                    db,
                    tenant_id=tenant_id,
                    agent_id=agent.id,
                    status=message.status,
                    message=message.text[:500],
                )
        except Exception:
            logger.warning("run: could not record an agent message", exc_info=True)
            return
        try:
            await db.commit()
        except Exception:
            # A failed COMMIT leaves nothing to salvage but the transaction
            # itself, so here the session-wide rollback is the only option.
            logger.warning("run: could not commit an agent message", exc_info=True)
            with contextlib.suppress(Exception):
                await db.rollback()
        await self._rebind(db, tenant_id)

    @staticmethod
    async def _rebind(db: AsyncSession, tenant_id: uuid.UUID) -> None:
        """Re-pin the RLS GUC after a commit -- it is transaction-local, so every
        statement after a commit would otherwise run unbound (RLS fails closed)."""
        await db.execute(
            sql_text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
        )


def register(contrib: object) -> None:
    contrib.add_runtime(NanoclawRuntime)  # type: ignore[attr-defined]
