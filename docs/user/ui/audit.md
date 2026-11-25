# Audit

**Route:** `/audit` · **Settings** section

## What it is

The **compliance archive**: a filterable, optionally integrity-checked chain of
governed events (who did what, when, under whose approval).

## What it is for

- Prove what happened after the fact
- Export evidence
- Detect tampering when HMAC chaining is enabled

## Where you are in the flow

```text
Day-to-day ops → Activity (live) · ★ Audit (evidence / compliance)
```

Use [Activity](activity.md) to debug today. Use Audit when someone asks “show me
the record” weeks later.

## What you do here

1. Open **Audit**; check integrity banner if shown.
2. Filter the event chain. The **Run** and **Chat** filters narrow it to one
   agent run or one Copilot chat; a link such as `/audit?run_id=…` opens the
   page already filtered.
3. Export from the UI or include audit data in your [Postgres backup](../../BACKUP_RESTORE).

## Tool events

Every `tool.call` event names the run it belongs to (and the chat, for Copilot
chats). Each dispatched call also gets a `tool.result` event with its outcome
(`ok` or `error`) and its duration.

- A `tool.call` event stores the call's arguments. The exception is a call
  to an edition tool: its `tool.call` event names only the tool, and its own
  `edition_tool.called` event records the call without arguments or output.
- A built-in oc8 tool that runs (a knowledge search, a budget overview) is
  recorded as `allow` with its `tool.result`, also on a Copilot chat with no
  connection.
- A `tool.result` event never stores the tool's output or the call's
  arguments. It holds the tool, the run (and chat), the connection (omitted
  for oc8's built-in tools), the outcome and the duration in milliseconds.

Runs on the in-process engine whose connection cannot be resolved at session
start record no per-call result event; isolated and gateway runs record the
outcome as `error`.

## Where work goes next

| Goal | Next |
|------|------|
| Live debugging | [Activity](activity.md) |
| Backup of the database | [Install → Backup](../install-and-maintain/index.md) |

## Related

- [Governance and approvals](../governance-and-approvals.md)
- [Backup and restore](../../BACKUP_RESTORE)
