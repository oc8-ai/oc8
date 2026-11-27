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

- A `tool.call` event stores the call's arguments. On in-process and
  isolated runs, the model's `justification` for the call is removed from
  them first. An approval card shows the justification instead. The exception
  is a call to an edition tool: its `tool.call` event names only the tool, and
  its own `edition_tool.called` event records the call without arguments or
  output.
- A built-in oc8 tool that runs (a knowledge search, a budget overview) is
  recorded as `allow` with its `tool.result`, also on a Copilot chat with no
  connection.
- Such a tool can run although the connection frame refused it, because that
  refusal concerns the run's connection, not the built-in tool. Its row
  carries `basis: core_read_outside_frame`. The row also holds the frame's own
  verdict under `pdp`, and its reason starts with
  `core read run outside the connection frame`. That tells it apart from an
  `allow` the policy gave.
- A `tool.result` event never stores the tool's output or the call's
  arguments. It holds the tool, the run (and chat), the connection (omitted
  for oc8's built-in tools), the outcome and the duration in milliseconds.

Runs on the in-process engine whose connection cannot be resolved at session
start record no per-call result event; isolated and gateway runs record the
outcome as `error`.

A `web_search` call's `tool.result` also holds the search provider and the
number of hits (`provider`, `count`); the query itself is in the `tool.call`
arguments.

## Chat events

| Action | Written when | Holds |
|--------|--------------|-------|
| `chat.feedback` | Someone rates an answer | `rating` (`up`/`down`), `note` (a rating sent through the API can carry one), the `model` that wrote the answer, the chat, message, run, agent and department |
| `chat.audio.transcribed` | A dictation was turned into text | provider, model, `seconds`, `bytes` — never the audio or the text |
| `chat.audio.spoken` | An answer was read aloud | provider, model, `characters` |
| `chat.audio.speech_failed` | Reading aloud failed at the provider | the error's kind |
| `chat.audio.allowance_overshoot` | The speech service reported a dictation more than ten times longer than its upload size suggested | `reserved_seconds`, `reported_seconds` |

All of them name the chat's agent, so the rule for personal agents applies:
for a personal agent's chat, content such as a rating's `note` or a search
query is shown only to the person it belongs to.

## Where work goes next

| Goal | Next |
|------|------|
| Live debugging | [Activity](activity.md) |
| Backup of the database | [Install → Backup](../install-and-maintain/index.md) |

## Related

- [Governance and approvals](../governance-and-approvals.md)
- [Backup and restore](../../BACKUP_RESTORE)
