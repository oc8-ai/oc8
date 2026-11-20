# Handoffs

**Route:** `/handoffs`

## What it is

A **handoff** is a controlled transfer of work from one department to another —
like shipping a folder with a cover sheet: what it is, who may open it, and what
must already be true.

## What it is for

- Move work across team boundaries without losing context
- Enforce gates (someone must accept; or a human must approve in My work)
- Keep provenance: where did this work come from?

## Where you are in the flow

```text
Department A finishes a stage → ★ Handoff created → Department B accepts
  → (optional gate → My work) → B's agents continue
```

If you use [Flows](flows.md), each stage often **is** a handoff under the hood.

## What you do here

1. Open **Handoffs**; filter incoming / outgoing / gated.
2. Open a handoff detail.
3. **Accept**, **Reject**, or complete as your role allows.
4. If gated for a person, the decision may also appear in [My work](my-work.md).
5. Under **Handoff types**, delete a type nothing uses. A type that handoffs
   were made with, or that a contract binding or a live flow still names, is
   kept, and the message says which.

## Where work goes next

| Action | Next |
|--------|------|
| Accept | Work appears for the receiving department / agents |
| Reject | Stays with sender / marked failed per rules |
| Gate needs human | [My work](my-work.md) |
| Part of a pipeline | Track overall progress in [Flows](flows.md) |

## Handoffs vs Flows

| | Handoffs | Flows |
|--|----------|-------|
| Question | “Is this one transfer OK?” | “What is the whole multi-step process?” |
| Cadence | Live decisions | Design once, run many times |

→ [How work moves](how-work-moves.md)

## Related

- [Departments](departments.md) · Collaboration tab
- [Flows](flows.md)
- [My work](my-work.md)
