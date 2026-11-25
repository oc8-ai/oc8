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
(`ok` or `error`) and its duration. Neither event stores arguments or content.
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
