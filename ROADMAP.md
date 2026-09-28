# Roadmap

This roadmap is directional, not a promised delivery schedule — priorities can
shift as we learn from real self-hosted deployments. It covers oc8, the
open-source, self-hostable core. If you want to extend oc8 today without
touching core, the best path is the
[capa system](capas/README.md) — drop a folder in `capas/`, and data-only
capa types (department templates, agent templates, skills, flows, tool packs)
install and run immediately with no code and no special trust level required.

## Shipped

### ✅ Agents, departments, and governed operations

The core operating model is in place: agents, departments, roles, skills,
approvals, and handoffs, plus team-lead delegation, run cancel/interrupt, and
live run streaming to the UI so a human can watch and intervene while an agent
works. A guided quickstart installer and an in-app onboarding wizard take a
fresh instance from empty database to a working department and agent.

### ✅ Provider-neutral model routing

Agents aren't locked to one vendor. The model router supports local and cloud
providers side by side, including an OpenAI-compatible adapter, so swapping a
model doesn't mean migrating agent state or rewriting integrations.

### ✅ Knowledge, memory, and cost control

Agents can be grounded in an organization's own documents through knowledge
bases with retrieval-augmented generation, carry memory across runs, and
operate inside per-agent and per-department budgets with cost tracking, so
autonomy doesn't come with an open-ended bill. A department-scoped
prompt-response cache — on by default, toggleable per department — serves a
repeated question from cache instead of a second model call, with the
tokens saved shown on the Cost page.

### ✅ Plugin framework

Connectors, skills, channels, and runtimes are all plugins, not hard-coded
integrations, and this isn't just an internal pattern — real plugins already
ship in the box: Google Drive and S3 knowledge connectors, Telegram and
WhatsApp approval channels, Gitea and Odoo MCP integrations, a sandboxed coding
runtime, and an OpenAI-compatible model provider. Each plugin declares the
permissions it needs and an operator has to explicitly install (and, for
code-shipping plugins, enable) it per instance.

### ✅ MCP tool gateway

Tool access for agents runs through a governed Model Context Protocol gateway
rather than direct, unmanaged calls, so an operator can see and control what
tools an agent can reach.

### ✅ Guardrail presets and department workspaces

Departments are scoped workspaces with their own tool allowlists. Guardrail
presets let a tool-pack plugin ship named, ready-made permission sets — for
example, a preset that always requires human approval for a sensitive action
regardless of transaction value — so an operator can apply a sound default in
one click instead of hand-assembling a policy.

### ✅ Runtime isolation

An agent can run in-process for simplicity or in an isolated container for
stronger blast-radius containment, selectable per agent. The isolated path
works against both Docker and Podman, so self-hosters aren't tied to a single
container runtime.

### ✅ Audit trail with optional hash chain

Every governed action is recorded with clear attribution, and the trail can
optionally be protected by an HMAC-keyed hash chain, so an operator can detect
if audit history was tampered with after the fact.

### ✅ Backup and restore

An administrator can export an instance's data as a single encrypted archive
from Settings and restore it later, on the same instance or a different one,
in a single all-or-nothing transaction — the kind of thing you want to have
rehearsed before you actually need it.

## In progress or next

### 🟡 Sandboxed execution for community-trust plugins

Right now only plugins marked `first_party` or `verified` are ever imported;
a plugin at the more open `community` trust level registers but its execution
path is a stub. Shipped so far: the plugin discovery, manifest, and
installation machinery already treats `community` as a real trust level and
routes it down a separate, sandboxed path. Next: implement that sandboxed
worker so a community-trust plugin can actually run its code, which is what
turns the plugin system from "extensible by us" into "extensible by anyone."

### ⚪ More OAuth providers for knowledge connectors

The OAuth connector framework and secret store are built, and Google is fully
wired end-to-end (the Google Drive connector authorizes, refreshes, and syncs
through it). The natural next step is registering more providers — starting
with ones like Slack or Microsoft — so self-hosters aren't limited to Google
when connecting a business tool as a knowledge source.

### ⚪ Interactive agent loop latency

Office agents should feel closer to Cursor / Claude Code / Hermes on
time-to-first-token and per-step wall clock, without weakening governance.
Not part of harness Packages 11–12 (code mode / suite tuning). First ship a
p50/p95 step-time + TTFT baseline on both runtimes, then attack measured
overhead: prefix-cache stability, fewer synchronous extra model calls,
tool/MCP round-trips, isolated cold start, and Live Log streaming perception.
Tracked in the office-agent harness design §12 follow-ups.
