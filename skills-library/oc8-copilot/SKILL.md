---
name: oc8-copilot
description: >
  Configure and manage a live oc8 tenant (departments, agents, missions,
  guardrails, triggers, plugins, integrations) through oc8's external Copilot
  MCP gateway, and turn a plain-language request like "set up a support team
  that escalates billing issues over €200" into the right sequence of
  proposals. Use this whenever the user gives you an oc8 API key
  (`oc8_ak_...`), an oc8 tenant URL, or asks you to set up, configure, hire,
  connect, automate, or change guardrails/permissions in their oc8 instance
  -- even if they just describe the outcome they want and never mention
  "Copilot" or "MCP" by name.
---

# oc8 Copilot

oc8 is an agent platform. Its admins increasingly hand you (an external AI
client — Claude Desktop, claude.ai, a custom integration) their own oc8 API
key and expect you to drive the tenant's configuration directly, the same
way oc8's own in-product "Copilot" dock does. This skill is that other
Copilot: same underlying capability set, reached from outside oc8 over MCP.

## Connecting

1. The user needs a running oc8 instance and an oc8 API key. If they don't
   have oc8 running yet, or don't know how to create a key or point you at
   the gateway, walk them through `references/getting-started.md` instead
   of assuming either is already in place — it covers self-hosted install,
   minting a key under **Settings → API keys**, and configuring Claude
   Desktop / claude.ai / ChatGPT to reach `/mcp/external`.
2. Add an MCP server pointing at `https://<their-oc8-host>/mcp/external`,
   authenticated with that key as a bearer token
   (`Authorization: Bearer oc8_ak_...`).
3. You'll see five tools: `copilot_propose`, `copilot_list_proposals`,
   `copilot_review_proposal`, `copilot_apply_proposal`,
   `copilot_reject_proposal`. That's the whole surface — read on for what
   they let you build and how to sequence them.

A key is exactly as powerful as the oc8 member who created it — never more.
If a call 403s with "missing permission", that member's own oc8 role
doesn't allow it; tell the user rather than retrying.

## The one rule that matters more than any other

**Never invent a UUID.** This gateway has no tool to list existing
departments, agents, plugins, integrations, or connections, and applying a
`department.create` or `agent.create` operation does not hand you back the
ID of what you just created — oc8 deliberately keeps this surface
secret-blind and read-free, so the only two places an ID can come from are
the human's own memory and the oc8 UI itself.

Concretely, this changes how you work:

- Before any operation that references an existing `agentId`,
  `departmentId`, `pluginId`, `integrationId`, or `connectionName`, ask the
  user for it if they haven't given it to you. Point them at the relevant
  oc8 screen — the ID is visible in the browser URL (e.g.
  `/departments/<id>`, `/agents/<id>`) or copyable from the entity's detail
  page. Never guess, never reuse an ID from a previous unrelated
  conversation, never pattern-match a plausible-looking UUID.
- When you `department.create` or `agent.create`, the proposal you get back
  after applying tells you *that it succeeded*, not the new entity's ID. If
  the very next thing you want to do needs that ID (e.g. hire an agent into
  the department you just created), tell the user the entity now exists and
  ask them to open it in the oc8 UI and hand you its ID before you continue
  — don't stall silently and don't fabricate one to keep going.
- `plugin.enable` and `integration.prepare` need IDs for things that were
  already installed/added in the oc8 UI beforehand — Copilot can turn them
  on, not conjure them into existence. If the user wants a plugin that
  doesn't seem to be installed yet, say so and point them at **Capas** in
  the oc8 UI (see `docs/user/integrations/index.md` for the catalog of what
  capas exist, if you have repo access, or just ask the user what's
  installed).
- `agent.guardrail.set`'s `connectionName`/`function` must match a tool the
  agent's department actually has access to, spelled exactly as oc8 knows
  it (e.g. `github`/`merge_pull_request`). Ask the user, or check the
  agent's tool grants in the oc8 UI, rather than guessing a plausible name.

Getting this wrong doesn't fail loudly with a clear error — `copilot_propose`
just returns `"invalid copilot proposal"` for a bad reference, same as for a
malformed field. Ask first; it's cheaper than a guessing loop.

## Turning what someone asks for into proposals

Most requests decompose into one or more of the seven operation types below
(full field reference: `references/operations.md`). Read the request for
its real intent, map it onto this list, and ask only for the IDs/names you
actually need — don't interrogate the user for things you can infer.

| The user wants... | Operation(s) |
|---|---|
| A new team for something oc8 doesn't have yet | `department.create`, then `agent.create` once you have the new department's id |
| A new agent doing a specific job | `agent.create` (needs an existing `departmentId`) |
| An agent's goal/focus changed | `agent.mission.set` (needs the agent's id) |
| Work to happen on a schedule or in response to an event | `trigger.create` (`kind: "cron"` with `cronExpression`, or `kind: "event"` with `eventSource`/`eventType` — ask which events oc8 already knows about rather than inventing one) |
| A capability/tool pack turned on for the tenant | `plugin.enable` (needs an already-installed plugin's id) |
| A third-party system wired up | `integration.prepare` — this only references an integration that already exists in oc8; it never carries credentials. Real setup (API keys, OAuth) still happens in oc8's normal integration flow, outside Copilot. |
| "Let the agent do X automatically", "X always needs my OK first", "X only below €500" | `agent.guardrail.set` with `decision` = `self_sufficient` / `approval_required` / `with_limits` respectively (see below) |

**Guardrail decisions**, the part people phrase in the most different ways —
map plain language onto exactly one of four states:

- *"never let it do X"* → `decision: "not_allowed"`
- *"it can just do X"* → `decision: "self_sufficient"`
- *"X is fine but only under some condition"* → `decision: "with_limits"` +
  `conditions` (e.g. an amount threshold)
- *"X always needs a human to sign off"* → `decision: "approval_required"`

A single natural-language ask often needs several operations in the right
order. "Set up a support team that can refund under €50 itself but needs me
for anything bigger" is: `department.create` → (get the new id from the
user) → `agent.create` → (get the new agent's id) → `agent.guardrail.set`
with `decision: "with_limits"` for the refund tool, conditioned on the
amount. Walk it step by step rather than trying to cram it into one
`copilot_propose` call — operations inside one proposal can't reference IDs
created by earlier operations in the *same* proposal, so a create-then-use
sequence is always at least two round trips.

## The propose → review → apply loop

1. **`copilot_propose`** with an `operations` array (each item
   `{"type": "...", ...fields}`). This validates and stores a `draft`
   proposal — nothing in the tenant changes yet. It returns the proposal id,
   status, and (for each operation) the closed set of references it carries
   — never the free-text you sent (mission text, task descriptions, etc.
   don't come back).
2. **Show the user what you're about to do** before applying — summarize
   the operations in plain language, not just the raw JSON. This mirrors
   oc8's own UI, which never applies a Copilot proposal without the human
   reviewing it first; don't skip that step just because you're outside the
   UI.
3. **`copilot_apply_proposal`** once they confirm. If the target changed
   since you proposed it (someone edited the agent in the UI meanwhile,
   say), the whole proposal comes back rejected as stale — re-propose from
   current state rather than retrying blindly.
4. **`copilot_reject_proposal`** if the user changes their mind, or
   `copilot_list_proposals` / `copilot_review_proposal` to look up drafts
   you (or someone else) made earlier in this tenant.

Pass the same `idempotencyKey` (any UUID you generate) if you need to retry
a `copilot_propose` call after a network hiccup — it's safe to resend.

Applying a proposal is all-or-nothing: every operation in it lands, or none
do. If you're unsure whether the user wants a batch of changes atomically or
independently, ask — and when in doubt, keep proposals small (one team, one
agent, one guardrail) so a rejection or a stale check never blocks an
unrelated change.

## Background reading

- `references/getting-started.md` — how to install oc8 from scratch and
  connect this skill to it over MCP, for a user who doesn't have either set
  up yet.
- `references/operations.md` — every field of all seven operation types,
  with example payloads.
- `references/chatgpt-instructions.md` — a self-contained version of this
  skill for pasting into a ChatGPT Custom GPT's instructions, for teams
  standardizing on a different assistant.
- oc8's own docs, if you have repo access: `docs/user/ui/copilot.md` (what
  the in-app Copilot is, for context) and `docs/user/integrations/index.md`
  (the capa/plugin catalog — what's installable, so you don't propose
  enabling something that was never built).
