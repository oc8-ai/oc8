# Daily workflow

How operators use oc8 after the instance is running. Read
[How work moves](ui/how-work-moves.md) and the [UI guide](ui/index.md) for
screen-by-screen detail.

## Morning setup (once per organisation)

1. **[Capas](ui/capas.md)** — Install extensions; enable; finish MCP setup.  
2. **[Credentials](ui/credentials.md)** — if secrets are shared across tools.  
3. **[Departments](ui/departments.md)** — frames: which tools and KBs this team may use.  
4. **[Agents](ui/agents.md)** + **[Skills](ui/skills.md)** — hire, mission, model, autonomy.  
5. **[Models](ui/models.md)** — ensure a usable LLM is assigned.

## Starting work

| Trigger | Where |
|---------|--------|
| Manual run / chat | [Agent](ui/agents.md) detail |
| See the floor | [Office](ui/office.md) |
| Department board | [Departments](ui/departments.md) Overview |
| Multi-stage process | [Flows](ui/flows.md) |
| Schedule / event | Configured on the agent or capa |

## While a run is active

- **[Office](ui/office.md)** / agent **Live Log** — status and transcript  
- **[Activity](ui/activity.md)** — operational feed  
- **Waiting on a human** → **[My work](ui/my-work.md)** (header badge)

## Handling approvals

Approvals are **not** a separate menu. Open **My work**:

1. Read the proposed action.  
2. Approve / reject / answer.  
3. Agent continues or stops.

Cross-team packages: also check **[Handoffs](ui/handoffs.md)**.

Details: [Governance and approvals](governance-and-approvals.md).

## Knowledge and memory

- **[Knowledge](ui/knowledge.md)** — ingest and attach bases.  
- Agent **Memory** tab — long-lived facts (may need approval).

## Copilot

Use the floating **[Copilot](ui/copilot.md)** to get work done or draft config;
you always review setup changes before applying.

## End of day

- Skim **[Activity](ui/activity.md)** and **[Costs](ui/costs.md)**.  
- Pause agents that should not run overnight.  
- Compliance export: **[Audit](ui/audit.md)**.

## Related

- [UI guide](ui/index.md)  
- [Integrations](integrations/index.md)  
- [Quickstart](quickstart.md)  
