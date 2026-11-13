# Flows

**Route:** `/flows`

## What it is

A **flow** is a multi-stage process across departments — a recipe: stage 1 in
Support, stage 2 in Finance, stage 3 back to Support, and so on.

Stages are usually linked by [Handoffs](handoffs.md). Live validation shows
whether the next stage can start.

## What it is for

- Encode end-to-end business processes (not just one agent task)
- See which stage a pipeline run is in
- Reuse the same process for many cases

## Where you are in the flow

```text
Design Flow (once) → start a Flow run → stages execute / pause
  → Handoffs + My work for gates → Flow completes
```

You are here when designing or monitoring a **pipeline**, not when answering a
single approval (that is [My work](my-work.md)).

## What you do here

1. Open **Flows**; create a flow or open an existing one.
2. Build stages on the canvas (which department / what handoff).
3. Start a run when ready.
4. Watch stage status; open blocked stages via Handoffs or My work.
5. **Cancel run** on a run that should stop: its open handoffs are rejected and
   any agent run working on them is stopped. Completed stages stay.
6. **Archive** a flow you no longer use: it leaves the list and cannot be
   started again, while runs already going continue. Publishing the flow again
   brings it back.

## Where work goes next

| Situation | Next |
|-----------|------|
| Stage waiting on transfer | [Handoffs](handoffs.md) |
| Stage waiting on person | [My work](my-work.md) |
| Stage is agent work | [Agents](agents.md) / department board |
| Need tools for a stage | [Capas](capas.md) + department permissions |

## Related

- [How work moves](how-work-moves.md) — Flows vs Handoffs in plain language
- [Handoffs](handoffs.md)
- [Departments](departments.md)
