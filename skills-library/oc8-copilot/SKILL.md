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
3. You'll see 13 tools: five for the propose → review → apply loop
   (`copilot_propose`, `copilot_list_proposals`, `copilot_review_proposal`,
   `copilot_apply_proposal`, `copilot_reject_proposal`) and eight read-only
   lookups (`copilot_list_departments`, `copilot_get_department`,
   `copilot_list_agents`, `copilot_get_agent`, `copilot_list_plugins`,
   `copilot_get_plugin`, `copilot_list_integrations`,
   `copilot_list_connection_tools`). That's the whole surface — read on for
   what they let you build and how to sequence them.

A key is exactly as powerful as the oc8 member who created it — never more.
If a call 403s with "missing permission", that member's own oc8 role
doesn't allow it; tell the user rather than retrying.

## The one rule that matters more than any other

**Never invent a UUID.** The gateway now has read tools for departments,
agents, plugins/capas, integrations, and a connection's tool catalog —
`copilot_list_departments`, `copilot_get_department`, `copilot_list_agents`,
`copilot_get_agent`, `copilot_list_plugins`, `copilot_get_plugin`,
`copilot_list_integrations`, `copilot_list_connection_tools` — so most of
the time you can look an ID up yourself instead of asking the user for it.
Two gaps remain, and this rule is really about not papering over them:

- Applying `department.create` or `agent.create` still does not hand you
  back the new entity's ID — oc8 deliberately keeps proposal application
  read-free even now. Call `copilot_list_departments`/`copilot_list_agents`
  right after and find the row you just created (filter agents by
  `departmentId`, match on name); if the result is empty or ambiguous (e.g.
  two departments share a name), ask the user rather than guessing which
  row is the new one.
- `agent.guardrail.set`'s `connectionName`/`function` pair still has no
  direct existence check beyond `copilot_list_connection_tools` — that tool
  tells you the real function names one named connection exposes, but
  there's no call that confirms a `connectionName` itself is one a given
  agent's department actually has access to. Call
  `copilot_list_connection_tools` before proposing a guardrail, and if it
  comes back empty or without the function you expected, ask the user
  rather than guessing a name.

Concretely, this changes how you work:

- Before any operation that references an existing `agentId`,
  `departmentId`, `pluginId`/`capaId`, `integrationId`, or `connectionName`,
  try the matching `copilot_list_*`/`copilot_get_*` tool first. Only ask the
  user when the entity is too new to show up yet (created earlier in this
  same conversation, before you had a chance to re-list) or when the lookup
  comes back empty or ambiguous. Never guess, never reuse an ID from a
  previous unrelated conversation, never pattern-match a plausible-looking
  UUID.
- When you `department.create` or `agent.create`, the proposal you get back
  after applying tells you *that it succeeded*, not the new entity's ID —
  follow up with the read tools above rather than defaulting to asking the
  user, but still ask if that lookup doesn't clearly identify the new row.
- `plugin.enable` and `integration.prepare` need IDs for things that were
  already installed/added in the oc8 UI beforehand, or — for a capa —
  installed through `capa.install` first; Copilot can turn them on, not
  conjure them into existence. `copilot_list_plugins`/
  `copilot_list_integrations` show you what's actually there; if the user
  wants something that isn't, say so rather than proposing an operation
  that will fail at apply time.
- `agent.guardrail.set`'s `connectionName`/`function` must match a tool the
  agent's department actually has access to, spelled exactly as oc8 knows
  it (e.g. `github`/`merge_pull_request`). Check with
  `copilot_list_connection_tools` first, or ask the user, rather than
  guessing a plausible name.

Getting this wrong doesn't fail loudly with a clear error — `copilot_propose`
just returns `"invalid copilot proposal"` for a bad reference, same as for a
malformed field. Check first; it's cheaper than a guessing loop.

## Turning what someone asks for into proposals

Requests decompose into one or more of the 22 operation types below (full
field reference: `references/operations.md`). Read the request for its real
intent, map it onto this list, and ask only for the IDs/names you actually
need — try the read tools first (see "the one rule" above), and don't
interrogate the user for things you can infer or look up.

| The user wants... | Operation(s) |
|---|---|
| A new team for something oc8 doesn't have yet | `department.create`, then `agent.create` once you have the new department's id |
| An existing team's name/goal/icon changed | `department.update` |
| A team archived, or brought back | `department.delete` / `department.restore` |
| A new agent doing a specific job | `agent.create` (needs an existing `departmentId`) |
| An agent renamed | `agent.rename` |
| An agent's goal/focus changed | `agent.mission.set` (needs the agent's id) |
| An agent started, paused, or stopped | `agent.lifecycle.set` with `action` = `start` / `pause` / `stop` |
| An agent archived, or brought back | `agent.delete` / `agent.restore` |
| An agent's tool access narrowed, or a narrowing removed | `agent.narrowing.set` / `agent.narrowing.reset` |
| An agent moved to a different runtime | `agent.runtime.assign` |
| An agent's model switched | `agent.model.switch` |
| A skill granted to an agent | `agent.skill.assign` |
| Work to happen on a schedule or in response to an event | `trigger.create` (`kind: "cron"` with `cronExpression`, or `kind: "event"` with `eventSource`/`eventType` — ask which events oc8 already knows about rather than inventing one) |
| A new capa (plugin) added to the tenant | `capa.install` (needs the on-disk `diskPluginId`, not a database id) |
| A capability/tool pack turned on for the tenant | `plugin.enable` (needs an already-installed plugin's id) |
| A capa turned off | `capa.disable` |
| A capa's non-secret setup fields filled in | `capa.configure` — never for a capa with an MCP connection or any password/credential field; see `references/operations.md`'s scope note |
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
- `references/operations.md` — every field of all 22 operation types, with
  example payloads, plus the 8 read-only discovery tools' arguments and
  purpose.
- `references/chatgpt-instructions.md` — a self-contained version of this
  skill for pasting into a ChatGPT Custom GPT's instructions, for teams
  standardizing on a different assistant.
- oc8's own docs, if you have repo access: `docs/user/ui/copilot.md` (what
  the in-app Copilot is, for context) and `docs/user/integrations/index.md`
  (the capa/plugin catalog — what's installable, so you don't propose
  enabling something that was never built).
