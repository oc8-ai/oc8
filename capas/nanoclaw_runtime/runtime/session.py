"""Session layout for a nanoclaw-hosted run.

oc8 takes nanoclaw's HOST role, so it owns the folder layout described in
nanoclaw's docs/db-session.md: a group folder (CLAUDE.md, container.json,
working files) and a session folder (the two SQLite DBs and the heartbeat).

The group folder is mounted read-write into the container, so anything under it
is reachable from inside. It is deliberately per-RUN, not per-agent, and lives
BESIDE the session rather than nested inside it (see `group_dir` / `session_dir`)
for two separate reasons:

- nesting the session inside the group folder would let one run reach another
  run's session -- read its inbound/outbound DBs, inject a message, or forge
  its ack;
- a per-AGENT group folder would let nanoclaw's agent-runner use it for more
  than CLAUDE.md and container.json: it also scaffolds a memory tree there at
  boot (`/workspace/agent/memory/{index.md,system/definition.md}`, nanoclaw's
  `ensureMemoryScaffold()`) and reloads those files into the model's context on
  every later run. A per-agent folder would make that memory tree survive
  across runs -- a second, un-gated persistence channel a container could use
  to implant standing instructions for its agent's FUTURE runs, bypassing
  oc8's own approval-gated memory writes (`resolve_memory_write_approval`)
  entirely. Making the group folder per-run closes that: it dies with the
  session, same as everything else the container can write.

The cost is visible, not hidden: nanoclaw's own cross-run agent memory feature
no longer persists anything in oc8, because the folder it would persist into no
longer survives past one run. oc8 does not use that feature -- agents get
cross-run memory through oc8's own gated subsystem instead -- so this trades a
feature we do not use for a hole we do not want.

`platform_mounts` additionally nests the agent's own `container.json` and
`CLAUDE.md` back in read-only on top of the (now per-run) group folder,
mirroring upstream, so the container can read its own configuration without
being able to rewrite it mid-run.

The DBs themselves are created by nanoclaw's own code -- see provision_session.
Writing their schema here would fork it, and a pin bump would then break us
silently.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import tempfile
import uuid
from contextlib import closing
from typing import Any

from oc8.config import get_settings
from oc8.sandbox.naming import container_name
from oc8.sandbox.reaper import RUN_LABEL, TENANT_LABEL
from oc8.sandbox.types import BindMount, SandboxSpec

from .session_db import CHANNEL_TYPE, ROUTE_NAME

#: NEVER `__name__`. This package is called `runtime/` (the folder convention
#: for every `runtime_adapter` plugin), so `__name__` here is `runtime.session`
#: -- a name claude_code_runtime, codex_runtime and opencode_runtime would all
#: share the moment any of them grows a logger, collapsing four plugins onto
#: one logger namespace and destroying per-plugin attribution in production
#: logs (spec §5.2). The pinned name keeps the plugin identity that the folder
#: name no longer carries.
logger = logging.getLogger("oc8.plugin.nanoclaw_runtime.session")

_BRIDGE_IN_CONTAINER = "/app/oc8-mcp-bridge.mjs"


#: The name this bridge is registered under inside the container. The model
#: sees every tool from it as `mcp__<name>__<tool>`, so anything that TELLS an
#: agent a tool name has to agree with it -- see MCP_TOOL_PREFIX.
MCP_SERVER_NAME = "oc8"
MCP_TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"

def _chown_to_sandbox_user(path: str) -> None:
    """Make a control-plane-written path readable/writable by the sandbox uid.

    The control plane runs as root and creates this directory or file as root
    (see `provision_session`, `write_container_json`, `write_claude_md`).
    Pinning the CONTAINER's uid via `SandboxSpec.user` is not enough on its
    own: bind mounts carry HOST ownership, so a root-owned 0600 file (or a
    directory root did not open up to another uid) stays unreadable /
    unwritable to the sandbox uid no matter what `--user` the container runs
    as. This is what makes `settings.sandbox_user` and `SandboxSpec.user`
    actually consistent with each other, rather than a setting that only
    half-works. A no-op when sandbox_user is unset (the default: containers
    keep the image's own user, and control-plane-owned files are fine as they
    already are).

    The value's shape (empty, or ``uid``/``uid:gid`` with integer parts) is
    guaranteed by `oc8.config.Settings._validate_sandbox_user` -- a malformed
    setting fails the process at startup rather than as a raw exception here,
    mid-run. What is NOT guaranteed is that the chown itself succeeds: EPERM
    (this process not running as root, a restricted capability set, a
    filesystem that refuses chown) is a real, live possibility. Swallowing
    that silently would recreate the exact half-working state this function
    exists to close -- only invisibly, with the failure resurfacing later
    inside the container, disconnected from its cause. So it is logged
    (best-effort, like every other write in this module: one path failing to
    chown must not abort the run) rather than raised or silently ignored.
    """
    user = get_settings().sandbox_user
    if not user:
        return
    uid_s, _, gid_s = user.partition(":")
    try:
        os.chown(path, int(uid_s), int(gid_s or uid_s))
    except OSError as exc:
        logger.warning("could not chown %s to sandbox_user %s: %s", path, user, exc)


def agent_image() -> str:
    """The nanoclaw agent-runner image, read straight from the environment.

    Not a core `Settings` field: core (`oc8.config`) stays software-neutral --
    no vendor/product names -- so which container runtime this plugin drives,
    and what image it needs, is this plugin's own business, not core's.
    """
    return os.environ.get("OC8_NANOCLAW_AGENT_IMAGE", "nanoclaw-agent:latest")


def provisioner_image() -> str:
    """The throwaway session-provisioner image (see `provision_session`).

    See `agent_image` for why this is read here, in the plugin, rather than
    exposed as a core `Settings` field.
    """
    return os.environ.get("OC8_NANOCLAW_PROVISIONER_IMAGE", "nanoclaw-provisioner:latest")


def runner_src() -> str:
    """Host path of nanoclaw's agent-runner source tree (mounted at /app/src).

    Read from the environment for the same reason `agent_image` is -- it names a
    nanoclaw artifact, so it is this plugin's business, not core's -- but also
    because it is a HOST path, resolved by the docker daemon, not a path inside
    the backend container. The plugin's own `__file__` is therefore useless here:
    it would give `/app/capas/...`, which does not exist on the host. Only the
    deployment knows where the pinned checkout actually lives, so only the
    deployment can say. The default is the packaging convention.
    """
    return os.environ.get("OC8_NANOCLAW_RUNNER_SRC", "/opt/nanoclaw/agent-runner/src")


def skills_src() -> str:
    """Host path of nanoclaw's skills tree (mounted at /app/skills). See `runner_src`."""
    return os.environ.get("OC8_NANOCLAW_SKILLS_SRC", "/opt/nanoclaw/skills")


def bridge_src() -> str:
    """Host path of oc8's stdio<->HTTP MCP bridge. See `runner_src`."""
    return os.environ.get("OC8_NANOCLAW_BRIDGE_SRC", "/opt/oc8/oc8-mcp-bridge.mjs")


def group_dir(root: str, agent_id: uuid.UUID, run_id: uuid.UUID) -> str:
    """The container's writable "agent" folder: CLAUDE.md, container.json,
    working files -- and, if the agent-runner writes it, its own memory
    scaffold. Per-RUN and living beside the session it belongs to (not nested
    inside it, and not shared across runs of the same agent): mounted
    read-write, so anything left under it must die with the run that wrote it,
    never survive to implant an instruction in that agent's NEXT run. See the
    module docstring for why this folder does not persist across runs."""
    return os.path.join(session_dir(root, agent_id, run_id), "agent")


def session_dir(root: str, agent_id: uuid.UUID, run_id: uuid.UUID) -> str:
    """One run's session. Deliberately NOT under group_dir: that folder is
    writable from inside the container, and a run that can reach a sibling's
    inbound.db can inject a message or forge its ack."""
    return os.path.join(root, "sessions", str(agent_id), str(run_id))


def claude_home_dir(root: str, agent_id: uuid.UUID, run_id: uuid.UUID) -> str:
    """Backs the container's `/home/node/.claude` -- upstream nanoclaw's own mount
    for "Claude state, settings, skill symlinks" (container-runner.ts), and, more
    to the point, where the Claude Agent SDK stores the `.jsonl` transcript a
    RESUME leg reads back by session id (`claudeConfigDir()` in
    `providers/claude.ts` falls back to `$HOME/.claude` when `CLAUDE_CONFIG_DIR`
    is unset, which it is here -- `runtime.py` sets `HOME=/home/node` in the
    container env and nothing more).

    Without a persistent directory behind that path, every fresh container (a
    park-for-approval always tears the old one down, per `NanoclawRuntime._drive`)
    starts with an EMPTY, container-local `.claude`, so the SDK cannot find the
    prior session on resume. When that error lands before the model reproduces
    the approved tool call, the wake message is acked with no redelivery and the
    call is silently never made -- live-observed, task-5-report.md in this same
    hardening slice (n=2: one park/resume cycle lost the approved
    `create_record`, the other did not; same code path, opposite outcomes).

    Per-RUN, beside the session, for the same reason `group_dir` is (see this
    module's docstring): a resume leg is the SAME run, so per-run already
    supplies everything the SDK's own resume needs, and a per-AGENT folder here
    would reopen the exact un-gated persistence hole that decision closed for
    `group_dir` -- one run's container being able to leave state a LATER,
    unrelated run of the same agent would read back as its own SDK session.
    """
    return os.path.join(session_dir(root, agent_id, run_id), "claude-home")


#: Every tool the pinned harness OFFERS an oc8 agent. MEASURED, not read off a
#: source file, because reading a source file is how the first two versions of
#: the list below got it wrong. Reproduce by running the CLI out of
#: `nanoclaw-agent:latest` against a stub Anthropic endpoint with the flags the
#: runner passes (`query()` in the agent-runner's `providers/claude.ts`) and
#: reading the `tools` array of the `system`/`init` line it prints:
#:
#:     claude -p hi --output-format stream-json --verbose \
#:       --permission-mode bypassPermissions --dangerously-skip-permissions \
#:       --setting-sources project,user,local --mcp-config <servers> \
#:       --allowedTools <TOOL_ALLOWLIST + mcp__*__*> \
#:       --disallowedTools <SDK_DISALLOWED_TOOLS>
#:
#: Measured 2026-08-02 against CLI 2.1.197 / `@anthropic-ai/claude-agent-sdk`
#: 0.3.197 (two different numbers for the same pin: `claude --version` reports
#: the first, `/app/node_modules/@anthropic-ai/claude-agent-sdk/package.json`
#: the second -- an earlier version of this comment quoted the CLI number under
#: the package name `@anthropic-ai/claude-code`, which is neither).
#:
#: The runner's own `TOOL_ALLOWLIST` is NOT this list and must never be read as
#: one: `allowedTools` becomes permission ALLOW rules, it does not narrow the
#: surface (narrowing is `--tools`, which the runner never passes). Measured
#: both ways -- `Workflow`, `Monitor`, `PushNotification` and the four `Task*`
#: list tools are offered while appearing nowhere in `TOOL_ALLOWLIST`, and
#: `TeamCreate`/`TeamDelete`/`TodoWrite`/`ToolSearch` sit IN `TOOL_ALLOWLIST`
#: and do not exist in this CLI at all. Naming a tool that does not exist in a
#: deny rule is not free: the CLI answers on stderr, once per leg, with
#: `Permission deny rule "TeamCreate" matches no known tool -- check for typos.`
#: An entry that draws that line is an entry protecting nothing.
#:
#: `WaitForMcpServers` is in this list but is offered CONDITIONALLY: it appears
#: only while some MCP server is still `pending`, which is exactly when
#: `runtime._READINESS_BLOCK` tells the agent to call it (verified by giving the
#: probe a deliberately slow second server). `Agent` is not offered under that
#: name -- `Task` is -- but the CLI knows it as `Task`'s alias and does not warn
#: on it, so denying both costs nothing and survives an alias flip.
HARNESS_TOOL_SURFACE = (
    "Bash",
    "Edit",
    "Glob",
    "Grep",
    "Monitor",
    "NotebookEdit",
    "PushNotification",
    "Read",
    "SendMessage",
    "Skill",
    "Task",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskOutput",
    "TaskStop",
    "TaskUpdate",
    "WaitForMcpServers",
    "WebFetch",
    "WebSearch",
    "Workflow",
    "Write",
    "mcp__nanoclaw__add_mcp_server",
    "mcp__nanoclaw__add_reaction",
    "mcp__nanoclaw__ask_user_question",
    "mcp__nanoclaw__create_agent",
    "mcp__nanoclaw__edit_message",
    "mcp__nanoclaw__install_packages",
    "mcp__nanoclaw__send_card",
    "mcp__nanoclaw__send_file",
    "mcp__nanoclaw__send_message",
)

#: Harness tools an oc8 agent must not have. Not a tidiness measure -- each one
#: is either a governance hole or a dead end the model cannot tell apart from
#: the real thing:
#:
#: * `Agent`/`Task` spawn a sub-agent INSIDE the container. It inherits no
#:   frame, spends no budget anybody counts, and appears in no audit trail --
#:   precisely the ungoverned worker oc8's whole permission model exists to
#:   prevent. oc8 has its own answer for handing work on (`delegate_task`),
#:   which creates a real run for a real agent under its own frame.
#: * `Workflow` is that same hole with a fan-out multiplier, and it was missed
#:   the first time because it is not in the runner's `TOOL_ALLOWLIST`. Its own
#:   description: "Execute a workflow script that orchestrates multiple
#:   subagents deterministically… Workflows can spawn dozens of agents and
#:   consume a large amount of tokens; the user must request that scale". Those
#:   agents run in this container under no oc8 frame, and their model calls go
#:   out over `ANTHROPIC_BASE_URL` -- oc8's own LLM gateway, on this run's
#:   token. The only thing standing between an agent and that is a sentence in
#:   the tool description asking for opt-in, which is precisely the kind of
#:   guard the closing paragraph here says is not enough. It also "returns
#:   immediately with a task ID" and promises a later `<task-notification>`:
#:   nanoclaw awaits one `provider.query()` per message batch (`poll-loop.ts`)
#:   and oc8 tears the container down when the run ends, so that notification
#:   has nowhere to land even when the work succeeds.
#: * `SendMessage` addresses sibling agents in the container, not an operator,
#:   and answers otherwise with "No agent named 'operator' is currently
#:   addressable. Spawn a new one or use the agent ID." -- which reads like a
#:   setup problem, so a model retries it. Observed live 2026-07-29: ten of a
#:   lead's twenty-two steps went there before it gave up.
#: * `Skill` is the harness's own skill loader and cannot reach oc8's skills,
#:   which arrive over the bridge. A model that calls it is told the skill does
#:   not exist, and the procedure it was supposed to follow never loads.
#: * `PushNotification` "sends a desktop notification in the user's terminal.
#:   If Remote Control is connected, it also pushes to their phone." There is no
#:   terminal and no Remote Control behind a headless agent container, and the
#:   tool tells the model so in advance: "If the result says the push wasn't
#:   sent, that's expected -- no action needed." Its description also recommends
#:   itself for "you've hit something that needs their decision before you can
#:   continue", which is the exact situation this whole list exists for -- so it
#:   is a second, quieter answer to the question `request_decision` answers, and
#:   the model cannot tell that nobody was reached.
#: * `WebFetch`/`WebSearch` are the agent's only routes to the open internet,
#:   and this deployment deliberately has none: agent containers sit on the
#:   `agents` docker network, declared `internal: true` (docker-compose.yml, and
#:   `docker network inspect oc8_agents` reports `Internal: true`), whose whole
#:   point is that a container "can reach the control plane and NOTHING else --
#:   not the database, not the internet". `WebFetch` therefore cannot connect at
#:   all, and `WebSearch` is executed server-side by whatever endpoint the LLM
#:   gateway fronts, which nobody here has configured to serve it. If oc8 ever
#:   wants the web it belongs behind an oc8 tool with the SSRF-guarded fetcher
#:   the knowledge connectors already use, not behind a harness built-in that
#:   nothing audits.
#:
#: The names above are the harness's BUILT-IN tools. Its MCP server ("nanoclaw",
#: registered by the runner itself, `src/mcp-tools/index.ts`) offers nine more
#: as `mcp__nanoclaw__…`, and the first list did not cover a single one of them.
#: Measured 2026-08-02: Sina was asked for the oldest open helpdesk ticket, got
#: her oc8 skill, and then called `mcp__nanoclaw__ask_user_question`. The run
#: failed after 32s with "the agent container stopped responding" (session
#: 019fa4f7-…/019fc314-…). That tool BLOCKS in-container for up to 300s polling
#: `inbound.db` for a question response, and oc8 never writes one -- so the
#: container simply stops answering until the executor gives up.
#:
#: The rest of that server is judged one by one, because two of the nine work:
#: * `send_message` DOES reach the operator here -- `write_routing` creates the
#:   `operator` destination, and `messages.to_agent_message` turns the resulting
#:   `chat` row into a real agent message. Offered.
#: * `send_card` likewise: `messages._text_of` has a branch for exactly its
#:   `fallbackText`/`card.description`/`card.title` shape. Offered.
#: * `create_agent`, `install_packages`, `add_mcp_server` all write a `system`
#:   row and answer "you will be notified when admin approves". `system` is in
#:   `messages._SILENT_KINDS`; nothing on the oc8 side reads those rows, so no
#:   notification is ever coming. `create_agent` is `Agent` again besides.
#: * `edit_message`, `add_reaction` write `chat` rows whose `operation` is in
#:   `messages._SILENT_OPERATIONS` -- dropped. A model that "corrects" a wrong
#:   answer with `edit_message` is told the edit was queued, and the wrong
#:   answer is what the operator keeps.
#: * `send_file` copies the file into `/workspace/outbox`, which oc8 never
#:   reads, and delivers only the accompanying text. The one part the model
#:   called it for is the part that silently disappears.
#:
#: SYNTAX, checked rather than assumed -- a deny entry that matches nothing is
#: this defect shipped again behind a comment claiming it is fixed. In the
#: pinned CLI an MCP tool is matched by its FULL `mcp__<server>__<tool>` name:
#: the rule matcher compares `rule.toolName` against `mcpInfo ?
#: mcp__server__tool : name`. Two further facts make this work at all, and both
#: were verified in that binary rather than taken on trust: the deny rules are
#: consulted BEFORE the permission mode, so they still bite under the
#: `bypassPermissions` the runner starts every session with; and the tool list
#: itself is filtered by them (`tools.filter(t => !denyRule(ctx, t))`), so a
#: denied tool is not offered rather than refused on use -- which is the whole
#: point. Then measured end to end: with this list in `settings.json` the probe
#: above offers 17 tools -- `Bash Edit Glob Grep Monitor NotebookEdit Read
#: TaskCreate TaskGet TaskList TaskOutput TaskStop TaskUpdate WaitForMcpServers
#: Write mcp__nanoclaw__send_card mcp__nanoclaw__send_message` -- against 31
#: without it. `ask_user_question` is gone, and so is every other name below.
#:
#: Deliberately NOT denied, having been checked:
#: * `WaitForMcpServers` -- a harness BUILT-IN, not an `mcp__nanoclaw__…` tool
#:   (it is nowhere in that server's registry). `runtime._READINESS_BLOCK` tells
#:   every agent to call it and two live runs did. A blanket `mcp__nanoclaw__*`
#:   would have been safe for it by luck; this list does not depend on luck.
#: * `TaskOutput`/`TaskStop` -- they LOOK like companions to the denied `Task`,
#:   but the CLI's alias map (`{Task: "Agent", BashOutput: "TaskOutput",
#:   KillShell: "TaskStop", …}`) shows they are the canonical names for reading
#:   and killing a background `Bash`. Denying them would take that away.
#: * `TaskCreate`/`TaskGet`/`TaskList`/`TaskUpdate` -- the CLI's own to-do list,
#:   `TodoWrite`'s replacement. In-session bookkeeping with no effect outside
#:   the container and nothing to mistake for reaching a human.
#: * `Monitor` -- `Bash(run_in_background)` with its stdout turned into
#:   notifications. It returns immediately, so it cannot wedge a run the way
#:   `ask_user_question` does, and it cannot outlive the container. No measured
#:   harm, so it stays: this list denies what was shown to break, not what looks
#:   unfamiliar.
#:
#: Naming them in the instructions worked (`SendMessage` attempts went from ten
#: to zero) but only for as long as the model reads carefully under pressure.
#: A tool that is not offered cannot be reached for at all.
DENIED_HARNESS_TOOLS = (
    "Agent",
    "Task",
    "Workflow",
    "SendMessage",
    "Skill",
    "PushNotification",
    "WebFetch",
    "WebSearch",
    "mcp__nanoclaw__ask_user_question",
    "mcp__nanoclaw__create_agent",
    "mcp__nanoclaw__install_packages",
    "mcp__nanoclaw__add_mcp_server",
    "mcp__nanoclaw__edit_message",
    "mcp__nanoclaw__add_reaction",
    "mcp__nanoclaw__send_file",
)

#: Named so a test can hold the line from the other side: widening the deny list
#: until one of these disappears breaks the agent, and should break the suite
#: first. `WaitForMcpServers` and `TaskOutput`/`TaskStop` are argued above;
#: `mcp__oc8__*` is the bridge, i.e. every tool the agent actually has a job to
#: do with. Everything here except the `mcp__oc8__…` pair is in
#: `HARNESS_TOOL_SURFACE`, and a test says so -- an earlier version of this list
#: "required" `TodoWrite` and `ToolSearch`, which this CLI does not have, so it
#: was holding a line in front of nothing.
REQUIRED_HARNESS_TOOLS = (
    "Bash",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "WaitForMcpServers",
    "TaskOutput",
    "TaskStop",
    "mcp__nanoclaw__send_message",
    "mcp__nanoclaw__send_card",
    "mcp__oc8__search_records",
    "mcp__oc8__request_decision",
)


def write_claude_settings(root: str, agent_id: uuid.UUID, run_id: uuid.UUID) -> str:
    """Deny the harness tools listed above, keeping whatever else is configured.

    Merged rather than written fresh: the provisioner puts nanoclaw's own
    `SessionStart` memory hook in this file, and replacing it would take the
    agent's memory tree away to save a handful of tool names. Called AFTER
    `provision_session` for the same reason.

    This file lands at `<claude-home>/settings.json`, mounted as
    `/home/node/.claude/settings.json`. That is the USER settings source, and
    the runner asks for it by name (`settingSources: ['project','user','local']`
    in its `providers/claude.ts`) -- which is why writing here is enough.

    KNOWN GAP, measured 2026-08-02 and deliberately not fixed here. Sharing a
    file with the provisioner means sharing its fate: if the CLI rejects the
    merged document, it rejects OUR half too, and it does so in silence. With
    the provisioner's real block (`SessionStart` matcher + a `hooks` array of
    `{type: command, ...}`) the probe in `HARNESS_TOOL_SURFACE` offers 17 tools,
    the deny list holding. Replace that block with a malformed one -- a matcher
    entry with no `hooks` array -- and the same probe offers all 31, every
    denied tool back, with no warning on stderr and nothing in the transcript.
    So the deny list is only as good as a file oc8 does not own the schema of.
    Not fixed because the shape the provisioner writes today is the valid one
    (verified against a retained `claude-home/settings.json`), and the honest
    fix -- putting oc8's permissions in the PROJECT settings source, which the
    runner also reads and the provisioner never touches -- is a mount and cwd
    change that wants its own slice rather than a schema validator here
    duplicating the harness's. Re-check this if a pin bump touches the hook
    format: the failure will look like a model reaching a denied tool, not like
    a broken settings file.
    """
    path = os.path.join(claude_home_dir(root, agent_id, run_id), "settings.json")
    settings: dict[str, Any] = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                settings = loaded
        except (OSError, ValueError):
            # An unreadable settings file is the provisioner's business, not
            # ours; overwriting it with only our own keys would hide that.
            logger.warning("settings.json at %s is unreadable, rewriting it", path)
    perms = settings.get("permissions")
    if not isinstance(perms, dict):
        perms = {}
    deny = [d for d in perms.get("deny", []) if isinstance(d, str)]
    for tool in DENIED_HARNESS_TOOLS:
        if tool not in deny:
            deny.append(tool)
    perms["deny"] = deny
    settings["permissions"] = perms
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)
    os.chmod(path, 0o600)
    _chown_to_sandbox_user(path)
    return path


def write_container_json(
    group: str,
    *,
    assistant_name: str,
    group_name: str,
    agent_id: uuid.UUID,
    mcp_url: str,
) -> str:
    """Write the per-run container.json nanoclaw's runner reads at startup.

    Deliberately absent: `model` (the LLM gateway ignores the requested model and
    uses the agent's ModelConfig -- a value here would be a second, lying source
    of truth) and any credential (this file is never deleted once written, so it
    outlives the container process whose token it would carry; the token travels
    in the container env instead).
    """
    config = {
        "provider": "claude",
        "assistantName": assistant_name,
        "groupName": group_name,
        "agentGroupId": str(agent_id),
        "maxMessagesPerPrompt": 10,
        "skills": [],
        "packages": {"apt": [], "npm": []},
        "additionalMounts": [],
        "mcpServers": {
            MCP_SERVER_NAME: {
                "command": "node",
                "args": [_BRIDGE_IN_CONTAINER],
                "env": {"OC8_MCP_URL": mcp_url},
            }
        },
    }
    path = os.path.join(group, "container.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
    os.chmod(path, 0o600)
    _chown_to_sandbox_user(path)
    return path


def write_claude_md(group: str, *, preamble: str) -> str:
    """oc8's standing instructions become the agent's CLAUDE.md -- nanoclaw's own
    mechanism for them, so skills arrive the way the harness already expects.

    Written atomically (temp file + rename) because this is rewritten on every
    leg of a resumed run (each `execute()` call writes it again, unconditionally)
    against the SAME per-run folder: a truncated CLAUDE.md read mid-rewrite would
    be a container running on half its instructions. `os.replace` is atomic
    within a directory, so a reader sees either the old file or the new one --
    true even though only one leg's container is ever live at a time, because it
    costs nothing to hold here and a future change to that assumption should not
    have to rediscover it the hard way. 0600 for the same reason container.json
    is: this is host-owned state.
    """
    path = os.path.join(group, "CLAUDE.md")
    fd, tmp = tempfile.mkstemp(dir=group, prefix=".CLAUDE.md.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(preamble)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        _chown_to_sandbox_user(path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def session_mounts_for(root: str, *, agent_id: uuid.UUID, run_id: uuid.UUID) -> list[BindMount]:
    """The three writable paths, all derived from tenant input -- so these, and
    only these, are what the sandbox allowlist has to guard.

    The third, `/home/node/.claude` (see `claude_home_dir`), is what makes a
    resume leg's container able to find the Claude Agent SDK's own transcript
    of the conversation it is resuming -- without it the SDK cannot locate its
    prior session across the teardown/restart a held-for-approval run always
    goes through, and an approved tool call can be silently lost.
    """
    return [
        BindMount(session_dir(root, agent_id, run_id), "/workspace", readonly=False),
        BindMount(group_dir(root, agent_id, run_id), "/workspace/agent", readonly=False),
        BindMount(claude_home_dir(root, agent_id, run_id), "/home/node/.claude", readonly=False),
    ]


def platform_mounts(
    *, runner_src: str, skills_src: str, bridge_path: str, group: str, claude_home: str
) -> list[BindMount]:
    """Read-only constants of this deployment, plus the agent's own config nested
    read-only on top of its writable folder -- upstream does the same, so the
    agent can READ its configuration and cannot rewrite it. A container that can
    edit its own CLAUDE.md is writing standing instructions for its agent's future
    runs.

    `settings.json` is nested the same way and for a sharper reason: it is the
    file that carries `DENIED_HARNESS_TOOLS`, and `/home/node/.claude` around it
    has to stay writable (the SDK keeps its resume transcript there, see
    `claude_home_dir`). The runner calls `provider.query()` once per message
    batch (`poll-loop.ts`) and each call spawns a fresh `claude` that re-reads
    the user settings -- so without this mount an agent holding `Write` or
    `Bash` could empty the deny list and be offered every tool again on the very
    next wake, inside the same container. Measured 2026-08-02: with the
    directory mount alone, `echo x >> /home/node/.claude/settings.json` inside
    the pinned agent image succeeds; with this mount it fails with `Read-only
    file system`, and the CLI still starts and still honours the deny list (the
    same probe recipe as `HARNESS_TOOL_SURFACE`, run both ways).

    `group` is the caller's own `group_dir(root, agent.id, run_id)` and
    `claude_home` its `claude_home_dir(root, agent.id, run_id)` -- both the SAME
    paths `session_mounts_for` already put through the session allowlist for its
    own (read-write) mounts, so nesting these read-only files on top of them
    needs no second validation pass. The three files themselves must already
    exist by the time this mount list is used: docker creates a missing
    bind-mount SOURCE as a directory, which would shadow the real file. The
    caller writes all three (`write_container_json`, `write_claude_md`,
    `write_claude_settings`) before building the sandbox spec that carries these
    mounts, so that is always true in practice.
    """
    return [
        BindMount(runner_src, "/app/src", readonly=True),
        BindMount(skills_src, "/app/skills", readonly=True),
        BindMount(bridge_path, _BRIDGE_IN_CONTAINER, readonly=True),
        BindMount(
            os.path.join(group, "container.json"),
            "/workspace/agent/container.json",
            readonly=True,
        ),
        BindMount(os.path.join(group, "CLAUDE.md"), "/workspace/agent/CLAUDE.md", readonly=True),
        # settings.json is deliberately NOT nested read-only here, though the
        # docstring above argues it should be and the probe that argued it was
        # right about the CLI. It was wrong about what runs BEFORE the CLI: the
        # agent-runner opens this file for writing at startup, so every single
        # run died on the first real attempt with
        #   [agent-runner] Fatal error: EROFS: read-only file system,
        #   open '/home/node/.claude/settings.json'
        # -- measured 2026-08-02 against the pinned image, run 019fc39d, which
        # failed 70 ms after the container started. The bypass that mount closed
        # is real (an agent holding Write can empty its own deny list and be
        # offered every tool on the next wake in the same container) but it is a
        # hardening, and a hardening that stops every run is worse than the hole
        # it closes. The honest fix is oc8's permissions in a settings source the
        # runner does not write -- the project-level file, which needs a mount
        # and a cwd change -- and that is its own slice.
    ]


def write_routing(session: str, *, run_id: uuid.UUID) -> None:
    """Give the agent one destination to answer to, and a default route.

    NOT optional (Task 0, Q1): with an empty `destinations` table the harness
    tells the model it cannot send anything, drops its reply as scratchpad, and
    still acks the message as completed -- a run that reports success with an
    empty output. Both rows are host-owned and live in inbound.db.

    Note: `sqlite3.Connection` as a context manager only commits/rolls back on
    exit, it does not close -- `closing()` is what actually releases the file
    descriptor, so the connect() is wrapped in that (with the `with conn:`
    nested inside for the commit semantics).
    """
    path = os.path.join(session, "inbound.db")
    with closing(sqlite3.connect(path, timeout=5.0)) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO destinations"
            " (name, display_name, type, channel_type, platform_id)"
            " VALUES (?, 'oc8 operator', 'channel', ?, ?)",
            (ROUTE_NAME, CHANNEL_TYPE, str(run_id)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO session_routing (id, channel_type, platform_id, thread_id)"
            " VALUES (1, ?, ?, NULL)",
            (CHANNEL_TYPE, str(run_id)),
        )


async def provision_session(
    root: str,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    run_id: uuid.UUID,
    image: str,
    driver: object,
    label: str = "agent",
) -> str:
    """Create the session folder and let NANOCLAW create the two DBs in it.

    The schema lives upstream (INBOUND_SCHEMA/OUTBOUND_SCHEMA in nanoclaw's
    src/db/schema.ts) and `ensureSchema()` is host code, so this runs a tiny
    provisioner image built from the same pinned checkout. One run, then gone.

    The provisioner image is built on `bun` (see provisioner/Dockerfile), so
    the interpreter named here is `bun`, not `node` -- a `node` binary that
    shells out to `bun` would exist only to keep this literal unchanged,
    which is a shim hiding what the image actually is.
    """
    session = session_dir(root, agent_id, run_id)
    group = group_dir(root, agent_id, run_id)
    claude_home = claude_home_dir(root, agent_id, run_id)
    os.makedirs(session, exist_ok=True)
    os.makedirs(group, exist_ok=True)
    os.makedirs(claude_home, exist_ok=True)
    # All three dirs are created by the control plane (root); the provisioner
    # and agent containers below write into them as `settings.sandbox_user`, so
    # without this they would be root-owned directories the sandbox uid
    # cannot write into -- see `_chown_to_sandbox_user`. `claude_home` must be
    # ready before the FIRST leg's container starts (not just a later resume
    # leg): the Claude Agent SDK writes its transcript there from the first
    # turn onward, so a resume has something to find.
    _chown_to_sandbox_user(session)
    _chown_to_sandbox_user(group)
    _chown_to_sandbox_user(claude_home)

    inbound = os.path.join(session, "inbound.db")
    outbound = os.path.join(session, "outbound.db")
    # A local, cheap stat on the session folder itself (already touched via
    # os.makedirs above, uncontested by any of this plugin's async I/O) --
    # not the kind of blocking call asyncio.to_thread exists to protect
    # against, same reasoning as the os.makedirs calls just above.
    if os.path.exists(inbound) and os.path.exists(outbound):  # noqa: ASYNC240
        # A resume leg: the session already exists and CARRIES the conversation
        # (nanoclaw keeps its SDK session id in session_state). Re-running the
        # provisioner over it would waste a container at best and clobber the
        # thing the resume exists to continue at worst.
        return session

    spec = SandboxSpec(
        image=image,
        name=container_name(label, "provision", run_id),
        labels={RUN_LABEL: str(run_id), TENANT_LABEL: str(tenant_id)},
        command=["bun", "/app/provision.js", "/session"],
        mounts=[BindMount(session, "/session", readonly=False)],
        network_disabled=True,
        mem_limit="256m",
        pids_limit=64,
        cpu_limit=0.5,
        user=get_settings().sandbox_user or None,
    )
    handle = await driver.provision(spec)  # type: ignore[attr-defined]
    try:
        code = await driver.wait(handle, timeout_s=60.0)  # type: ignore[attr-defined]
        if code != 0:
            logs = await driver.logs(handle)  # type: ignore[attr-defined]
            raise RuntimeError(f"session provisioning failed ({code}): {logs[-500:]}")
    finally:
        await driver.teardown(handle)  # type: ignore[attr-defined]
    return session
