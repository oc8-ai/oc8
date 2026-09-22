# Operation reference

The closed set of 22 operation types `copilot_propose` accepts, one
object per operation in the `operations` array: `{"type": "...", ...}`.
Every field below is exactly what oc8's backend validates
(`copilot/capabilities.py`) — extra fields are rejected, missing required
fields are rejected, and a bad reference (an id that doesn't exist, or
belongs to a different tenant) fails at `copilot_apply_proposal` time with a
generic "cannot be applied" error, not a field-level message. Get field
names and types right the first time; there's no partial-credit feedback.

This gateway also exposes 8 read-only tools (`copilot_list_departments` and
friends, documented in their own section below) that aren't part of
`copilot_propose` at all — call them directly via `tools/call`, no proposal
or review needed, to look up an id/name instead of asking the user for one.

None of these operations ever accept a secret, credential, or API key —
that's by design (see `SKILL.md`'s "one rule that matters more"). Anything
that needs a credential (`integration.prepare`) only carries a reference to
one, prepared through oc8's normal setup flow.

## `agent.mission.set`

Rewrites an agent's mission/goal text entirely — this replaces the mission,
it doesn't append to it.

```json
{
  "type": "agent.mission.set",
  "agentId": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "mission": "Triage inbound support tickets, resolve anything covered by the FAQ yourself, escalate billing disputes to a human."
}
```

- `agentId` — UUID of an existing, non-deleted agent. Ask the user for it.
- `mission` — 1 to 10,000 characters. Write the mission the way you'd write
  instructions for a new hire: what to do, what "done" looks like, when to
  stop and ask.

## `trigger.create`

Gives an agent a way to start working without a human clicking "run" —
either on a schedule or in response to an event oc8 already knows about.

```json
{
  "type": "trigger.create",
  "agentId": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "kind": "cron",
  "taskText": "Check for support tickets older than 24h with no reply and follow up.",
  "cronExpression": "0 9 * * *"
}
```

```json
{
  "type": "trigger.create",
  "agentId": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "kind": "event",
  "taskText": "A new GitHub issue was labeled 'bug' — investigate and comment with findings.",
  "eventSource": "github",
  "eventType": "issue.labeled"
}
```

- `agentId` — UUID of an existing agent.
- `kind` — `"cron"` or `"event"`.
- `taskText` — 1 to 10,000 characters describing what the agent should do
  when the trigger fires. This is the actual instruction the agent acts on
  each run, not a label — write it the way you'd write `agent.mission.set`'s
  `mission`.
- `cronExpression` — standard cron syntax, only meaningful for `kind:
  "cron"`.
- `eventSource` / `eventType` — only meaningful for `kind: "event"`. These
  must match an event oc8 has actually declared (installed capas register
  their own events — GitHub, Jira, etc. each bring their own catalog). Ask
  the user which event they mean, or check the oc8 UI's trigger setup,
  rather than guessing a source/type pair — an unrecognised pair fails at
  apply time.

## `plugin.enable`

Turns on a plugin (capa) that's already installed in this oc8 tenant, with
a specific set of permissions granted.

```json
{
  "type": "plugin.enable",
  "pluginId": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "grantedPermissions": ["read", "write"]
}
```

- `pluginId` — UUID of an already-installed plugin (see the oc8 UI's
  **Capas** screen, or `docs/user/integrations/index.md` for what capas
  exist at all). This operation cannot install a new plugin, only enable
  one that's already there.
- `grantedPermissions` — up to 64 strings; the plugin's own manifest defines
  what permission names it understands. Ask the user which permissions they
  want to grant rather than granting everything by default.

## `capa.install`

Installs a plugin (capa) from the catalog on disk into this tenant. This is
the only capa operation whose identifier is not a database id — nothing
exists to have one yet.

```json
{
  "type": "capa.install",
  "diskPluginId": "github_mcp"
}
```

- `diskPluginId` — 1 to 200 characters, the plugin's on-disk folder id (the
  catalog id `find_plugin` resolves, e.g. `"github_mcp"`) — not the UUID any
  other capa operation uses. Ask the user which capa they mean, or check
  oc8's **Capas** catalog screen, rather than guessing a folder name.

Installing does not enable the capa for use — follow up with `plugin.enable`
once you have the newly-installed capa's id (from `copilot_list_plugins`).

## `capa.disable`

Disables an already-installed, enabled capa.

```json
{
  "type": "capa.disable",
  "capaId": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "reason": "No longer needed for this workflow."
}
```

- `capaId` — UUID of an installed capa (`copilot_list_plugins`/
  `copilot_get_plugin`).
- `reason` — optional, up to 1,000 characters, recorded for audit purposes.

## `capa.configure`

Writes plain-text setup field values into an already-installed, enabled
capa's configuration — the non-secret half of the oc8 UI's capa setup form.

```json
{
  "type": "capa.configure",
  "capaId": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "values": {
    "base_url": "https://example.odoo.com"
  }
}
```

- `capaId` — UUID of an installed, enabled capa.
- `values` — up to 64 key/value string pairs, one per setup field the capa's
  manifest declares. Unknown keys, and missing required fields, are
  rejected.

**Scope note — this operation does not cover every capa.** Capas with an
MCP connection or any password/credential setup field must still be
configured through the in-app UI. If the capa's manifest declares even one
secret-shaped field, the *entire* operation is rejected — not just the
secret field — so never attempt to work around this by omitting the secret
key from `values`; tell the user to finish setup in oc8's own UI instead.

## `integration.prepare`

References a credential/configuration that was set up through oc8's normal
integration flow (outside Copilot), marking the integration as ready to
use.

```json
{
  "type": "integration.prepare",
  "integrationId": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "configurationRef": "9c858901-8a57-4791-81fe-4c455b099bc9"
}
```

- `integrationId` — UUID of an existing integration record.
- `configurationRef` — optional UUID pointing at a prepared configuration.
  Never a raw secret, never an API key, never a connection string — if the
  user hands you a credential directly, tell them it belongs in oc8's own
  integration setup UI, not in a message to you.

## `department.create`

Creates a new, empty department (team) — no tools, no members yet.

```json
{
  "type": "department.create",
  "name": "Support",
  "goal": "Resolve inbound customer tickets quickly and correctly.",
  "icon": "life-buoy"
}
```

- `name` — 1 to 200 characters.
- `goal` — up to 2,000 characters, defaults to empty.
- `icon` — up to 100 characters, defaults to `"building"`.

**Applying this does not return the new department's id.** If the next
step needs it (almost always — an empty department isn't useful on its
own), ask the user to open the new department in the oc8 UI and give you
its id before you propose anything that references it.

## `department.update`

Partially updates an existing department — only the fields you include are
changed; omit a field to leave it as-is.

```json
{
  "type": "department.update",
  "departmentId": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "name": "Customer Support",
  "goal": "Resolve inbound customer tickets quickly and correctly.",
  "icon": "life-buoy"
}
```

- `departmentId` — UUID of an existing, non-deleted department.
- `name` — optional, 1 to 200 characters.
- `goal` — optional, up to 2,000 characters.
- `icon` — optional, up to 100 characters.

## `department.delete`

Archives a department. If it still has agents, they're archived along with
it (an agent with no run history is deleted outright instead; one with run
history is archived, same as `agent.delete`'s own rule). An empty
department is deleted outright.

```json
{
  "type": "department.delete",
  "departmentId": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
}
```

- `departmentId` — UUID of an existing, non-deleted department.

## `department.restore`

Un-archives a department that was previously deleted. Fails if the
department is currently live (not archived) — there's nothing to restore.

```json
{
  "type": "department.restore",
  "departmentId": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
}
```

- `departmentId` — UUID of a currently-archived department.

## `agent.create`

Creates a new agent inside an existing department, with a name, an
optional role title, and an optional starting mission. Deliberately
minimal — no tools, no model choice, no guardrails yet; those come from a
human afterward through the normal Hire/agent-detail flow, or from your own
follow-up `agent.mission.set` / `agent.guardrail.set` operations once you
have the new agent's id.

```json
{
  "type": "agent.create",
  "departmentId": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "name": "Ticket Triage",
  "roleTitle": "Support Agent",
  "mission": "Read new tickets, answer FAQ-covered questions, tag anything else for a human."
}
```

- `departmentId` — UUID of an existing, non-deleted department.
- `name` — 1 to 200 characters.
- `roleTitle` — up to 200 characters, defaults to empty.
- `mission` — up to 10,000 characters, defaults to empty. You can also set
  this later with `agent.mission.set`.

**Applying this does not return the new agent's id either** — same caveat
as `department.create`. If your very next operation needs it (a guardrail,
a trigger), ask the user to fetch it from the oc8 UI first.

## `agent.rename`

Changes an agent's display name.

```json
{
  "type": "agent.rename",
  "agentId": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "name": "Ticket Triage Lead"
}
```

- `agentId` — UUID of an existing, non-deleted agent.
- `name` — 1 to 200 characters.

## `agent.lifecycle.set`

Starts, pauses, or stops an agent.

```json
{
  "type": "agent.lifecycle.set",
  "agentId": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "action": "pause"
}
```

- `agentId` — UUID of an existing, non-deleted agent.
- `action` — one of `"start"`, `"pause"`, `"stop"` (mapping to the agent's
  `running`/`paused`/`stopped` status). Fails if the agent is still
  `pending_approval` — an unhired agent has no lifecycle to set yet.

## `agent.delete`

Deletes (or archives, if it has run history) an agent — same rule
`department.delete` applies to the agents inside a deleted department.

```json
{
  "type": "agent.delete",
  "agentId": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
}
```

- `agentId` — UUID of an existing, non-deleted agent.

## `agent.restore`

Un-archives a previously deleted agent. Fails if the agent is currently
live.

```json
{
  "type": "agent.restore",
  "agentId": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
}
```

- `agentId` — UUID of a currently-archived agent.

## `agent.narrowing.set`

Overrides one or more of an agent's tool policies within its department's
frame — the same `PUT /agents/{id}/narrowing` payload shape the oc8 UI's
agent detail page writes. This can only ever *narrow* what the department
frame already allows, never grant more; a narrowing that would widen access
is rejected.

```json
{
  "type": "agent.narrowing.set",
  "agentId": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "narrowing": {
    "tools": {
      "github": {
        "enabled": true,
        "read": true,
        "modify": false
      }
    }
  }
}
```

- `agentId` — UUID of an existing, non-deleted agent.
- `narrowing` — an object keyed by `"tools"`, itself keyed by
  `connectionName`, each value a tool-policy override (`enabled`/`read`/
  `modify`/`approval_eur`/`only`/`approval_actions`/`conditions`, matching
  what the oc8 UI's narrowing editor writes). `copilot_get_agent` does not
  currently surface an agent's existing narrowing, so ask the user rather
  than guessing this shape from scratch — it's easy to get subtly wrong.

## `agent.narrowing.reset`

Removes one connection's narrowing override, falling back to whatever the
department frame allows for it by default.

```json
{
  "type": "agent.narrowing.reset",
  "agentId": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "connectionName": "github"
}
```

- `agentId` — UUID of an existing, non-deleted agent.
- `connectionName` — the one connection to reset. Fails if the agent has no
  override for that connection — there's nothing to reset.

## `agent.runtime.assign`

Assigns (or clears) the runtime plugin an agent executes under.

```json
{
  "type": "agent.runtime.assign",
  "agentId": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "runtimePluginId": "claude_code"
}
```

- `agentId` — UUID of an existing, non-deleted agent.
- `runtimePluginId` — the runtime plugin's id, or explicit `null` to clear
  the agent's runtime assignment. This key must always be present in the
  payload — an absent key is rejected outright, since "not sent" and
  "explicitly cleared" mean different things here.

## `agent.model.switch`

Changes which model config an agent uses.

```json
{
  "type": "agent.model.switch",
  "agentId": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "modelConfigId": "9c858901-8a57-4791-81fe-4c455b099bc9"
}
```

- `agentId` — UUID of an existing, non-deleted agent.
- `modelConfigId` — UUID of an existing model config. Fails if the model is
  incompatible with a subscription-backed agent that must stay
  manual-model-only.

## `agent.skill.assign`

Grants an agent a skill version, if the agent's department/narrowing
already satisfies that skill's own requirements (e.g. a knowledge base
grant). Already-assigned is treated as success, not an error.

```json
{
  "type": "agent.skill.assign",
  "agentId": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "skillVersionId": "9c858901-8a57-4791-81fe-4c455b099bc9"
}
```

- `agentId` — UUID of an existing, non-deleted agent.
- `skillVersionId` — UUID of an existing skill version.

## `agent.guardrail.set`

Sets the permission decision for one specific tool an agent's department
already grants it access to. This is the one operation that actually
touches authorization, so get the `connectionName`/`function` pair exactly
right — a name that doesn't match a real, granted tool fails at apply time.

```json
{
  "type": "agent.guardrail.set",
  "agentId": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "connectionName": "github",
  "function": "merge_pull_request",
  "decision": "approval_required",
  "conditions": []
}
```

```json
{
  "type": "agent.guardrail.set",
  "agentId": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "connectionName": "odoo",
  "function": "issue_refund",
  "decision": "with_limits",
  "conditions": [
    {
      "attribute": "amount",
      "datatype": "number",
      "operator": "lte",
      "value": 50,
      "then": "allow"
    }
  ]
}
```

- `agentId` — UUID of an existing agent.
- `connectionName` — the tool connection's name exactly as oc8 knows it
  (e.g. `github`, `odoo`, `jira`) — check the agent's tool grants in the
  oc8 UI if unsure, never guess.
- `function` — the specific tool/function name within that connection
  (e.g. `merge_pull_request`), again exactly as granted.
- `decision` — one of four states, and they are exhaustive — every request
  about "should the agent be allowed to..." maps onto exactly one:
  - `"not_allowed"` — the agent can never call this function.
  - `"self_sufficient"` — the agent can call it whenever it judges
    appropriate, no human involved.
  - `"with_limits"` — the agent can call it, but only within the bounds
    `conditions` describe (an amount threshold, an enum value, etc.);
    calls outside those bounds fall back to needing approval.
  - `"approval_required"` — every single call needs a human to say yes
    first, unconditionally.
- `conditions` — required (non-empty) only when `decision` is
  `"with_limits"`; must be empty for every other decision. Each condition:
  `attribute` (must be one the tool's own manifest declares — again, ask
  rather than guess if unsure), `datatype`, `operator`, `value`, and `then`.

Setting a guardrail here can only ever *narrow* what the agent's department
frame already allows — it can't grant access the department itself doesn't
have. If the user wants to loosen something beyond the department's frame,
that's a department-level change made through the normal oc8 UI, not this
operation.

## Read-only discovery tools

These 8 tools aren't operations — they're separate `tools/call` entries,
called directly, no `copilot_propose`/review/apply cycle involved, since
nothing changes. Use them to look up an id or name before an operation that
needs one, instead of asking the user or guessing.

| Tool | Purpose | Arguments | Example use |
|---|---|---|---|
| `copilot_list_departments` | List every department in the tenant (id, name, goal, icon), including archived ones. | none | Find the id of the "Support" department the user mentioned by name. |
| `copilot_get_department` | Get one department by id. | `departmentId` (required) | Confirm a department still exists, and read its current `goal`, before proposing `department.update`. |
| `copilot_list_agents` | List agents in the tenant, optionally filtered to one department. | `departmentId` (optional) | List every agent in a department to find the one named "Ticket Triage". |
| `copilot_get_agent` | Get one agent by id. | `agentId` (required) | Look up an agent's current `status` before proposing `agent.lifecycle.set`. |
| `copilot_list_plugins` | List every installed capa (plugin) in the tenant, with its id, name, type, and trust level. | none | Find the id of an already-installed "GitHub" capa to pass to `plugin.enable`. |
| `copilot_get_plugin` | Get one installed capa by id. | `pluginId` (required) | Confirm a capa install actually completed after `capa.install`. |
| `copilot_list_integrations` | List every catalog integration available to the tenant (id, name, category, connected). | none | Find the id of the integration the user wants `integration.prepare`d. |
| `copilot_list_connection_tools` | List the real function names a named MCP connection exposes. | `connectionName` (required) | Check that `"merge_pull_request"` is really a valid `function` for the `"github"` connection before proposing `agent.guardrail.set`. |

Note: `copilot_get_agent`/`copilot_list_agents` return the same summary
shape the operator UI's agent cards use (name, role, status, tools,
guardrails, department/model ids, ...) — they do not currently return an
agent's narrowing or runtime assignment, so those still need to come from
the user or the oc8 UI. `copilot_list_plugins`/`copilot_get_plugin` return
a capa's id/name/type/trust level/current version id, not its full setup
form.
