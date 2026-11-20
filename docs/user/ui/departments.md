# Departments

**Route:** `/departments` · Detail: `/departments/$id`

## What it is

A **team boundary** inside your organisation: who works together, which tools
they may use (the *frame*), and the **task board** for that team.

## What it is for

- Organise agents by real teams (Sales, Support, Finance, …)
- Set default tool policies for everyone in the team
- See work as columns: Backlog → In Progress → Awaiting Approval → Done

## Where you are in the flow

```text
Capas connected → ★ Department (frame + board) → Agents hire into it → runs & handoffs
```

Departments sit between **platform setup** and **day-to-day agent work**.

## Tabs on the detail page

| Tab | Purpose |
|-----|---------|
| **Overview** | Task board, agents in this team, recent activity |
| **Integrations & Permissions** | Which capa tools this department may use |
| **Collaboration** | How this team participates in handoffs: emitted events, intakes, and the contract bindings that turn an event into a handoff (remove one there to stop it) |
| **Settings** | Department-level options (e.g. prompt caching) |

## What you do here

1. Create or open a department.
2. On **Integrations & Permissions**, allow only the tools this team needs.
3. Hire or assign [Agents](agents.md) into the department.
4. Watch the board: items in **Awaiting Approval** usually need someone in
   [My work](my-work.md).

## Where work goes next

| Situation | Next screen |
|-----------|-------------|
| Need a worker | [Agents](agents.md) |
| Need a tool | [Capas](capas.md) then back to Integrations & Permissions |
| Pass work to another team | [Handoffs](handoffs.md) / [Flows](flows.md) |
| Human gate | [My work](my-work.md) |

## Related

- [Key concepts — Department](../key-concepts.mdx#department)
- [How work moves](how-work-moves.md)
