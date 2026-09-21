# Operation reference

The closed set of seven operation types `copilot_propose` accepts, one
object per operation in the `operations` array: `{"type": "...", ...}`.
Every field below is exactly what oc8's backend validates
(`copilot/capabilities.py`) — extra fields are rejected, missing required
fields are rejected, and a bad reference (an id that doesn't exist, or
belongs to a different tenant) fails at `copilot_apply_proposal` time with a
generic "cannot be applied" error, not a field-level message. Get field
names and types right the first time; there's no partial-credit feedback.

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
