# Capas — how oc8 extends

**Capas** (from *capabilities*) are installable extensions. They are how oc8 connects
to your stack, ships ready-made teams, and adds new behaviour — **without**
changing the core platform on every new tool or use case.

Think of oc8 as a **microkernel**: runs, approvals, secrets, and the MCP gateway
stay in core. Everything specific to Odoo, Microsoft 365, a helpdesk persona, or
a Telegram approval bot lives in a capa you install when you need it.

→ Screen-by-screen: **[Capas](../ui/capas.md)** in the app  
→ Build your own: **[Developer documentation](../../developer/index.md)**

---

## Why capas matter

| Without capas | With capas |
|---------------|------------|
| Fork core for every integration | Drop in a folder under `capas/` |
| One-size-fits-all agents | Hire templates tuned to Sales, Support, … |
| Hard-code OAuth per vendor | Each capa ships its own setup form |
| All tenants get everything | Install and enable **per organisation** |

You choose what is on your instance. Two oc8 deployments can look completely
different — same core, different capas.

---

## What a capa can add

Each capa declares one primary **type**. In practice many capas combine several
contributions (e.g. M365 tools **and** a knowledge connector in one package).

| Type | What you get | Typical examples |
|------|--------------|------------------|
| **tool_pack** | MCP connections to external systems (CRM, Git, issue trackers) | Odoo, GitHub, Jira, Microsoft 365 |
| **connector** | Knowledge sources — documents flow into RAG | Google Drive, S3 |
| **agent_template** | One hireable agent (mission, skills, guardrails) | Helpdesk agent, CRM sales agent |
| **department_template** | A whole team + starter frame | Vertrieb bundle |
| **skill** | Reusable procedures in the skill library | First-response playbook, human-decision step |
| **flow_template** | A pre-built multi-stage pipeline | Onboarding or escalation flows |
| **runtime_adapter** | Alternative way an agent runs (isolated CLI, sandbox) | Nanoclaw, Claude Code, OpenCode |
| **model_adapter** | Extra LLM provider wiring | Custom or regional providers |
| **approval_channel** | Send approval requests outside the UI | Telegram, WhatsApp |

**Data-only capas** (templates, tool packs, skills, flows) materialise when you
**enable** them — that is when you see and accept the permission consent.
**Code-carrying capas** (connectors, runtimes, channels) run trusted Python on
the control plane; only tenants that enabled them can use what they register.

---

## Flexibility in practice

### Mix tools from many vendors

One department can allow Odoo **and** GitHub **and** M365. Each tool pack adds
MCP connections; the **department frame** decides which connections and which
rights (`read` / `write` / `send`) agents may use. Agents never hold vendor
credentials — the gateway resolves secrets on the control plane.

### Start from a template or build from scratch

- **Hire** an `agent_template` capa → agent with mission and skills pre-filled.
- **Instantiate** a `department_template` → whole team in one step.
- Or create departments and agents manually and attach only the tool packs you need.

### Extend how agents run

Default runs use the built-in loop. **Runtime** capas swap the execution engine —
for example an isolated container with a CLI agent — while the same approvals,
audit, and MCP gateway still apply.

### Meet people where they are

**Approval channel** capas deliver the same approval record to Telegram or
WhatsApp, so on-call staff can approve without opening the UI.

### Grow the skill and flow library

**Skill** capas add playbooks operators assign to agents. **Flow template** capas
add repeatable pipelines (Handoffs chained in order) you can adopt and tune.

---

## What ships in this repository

Examples you can install from **Capas → Available** (exact list depends on your
build):

| Category | Capas (examples) |
|----------|------------------|
| Business tools | `microsoft365`, `google_workspace`, `odoo_mcp`, `hubspot_mcp`, `github_mcp`, `jira_mcp`, `gitea_mcp` |
| Multi-provider tool router | `treg_mcp` — one token, thousands of catalogued tools across dozens of providers via [Treg](https://github.com/superdesigndev/treg), without a dedicated capa per provider |
| Knowledge | `gdrive_source`, `s3_source` |
| Ready-made teams | `helpdesk_support_agent`, `crm_vertrieb_agent`, `vertrieb_bundle`, `engineering_dev_agent` |
| Skills | `helpdesk_first_response_skill`, `standard_skills`, `human_decision_skill` |
| Runtimes | `nanoclaw_runtime`, `claude_code_runtime`, `opencode_runtime`, `codex_runtime` |
| Approvals | `telegram_approvals`, `whatsapp_approvals` |

New capas appear when someone adds a folder under `capas/` — discovery is
automatic; restart is not required for install, though code changes need a
process restart.

---

## How you wire a capa (operator flow)

```text
Capas → install & enable (accept consent)
     → finish connection setup (OAuth, URL, secrets)
Departments → Integrations & Permissions → allow tools for the team
Agents → Access & Permissions → narrow if needed
Run a task → risky steps land in My work (or a messenger capa)
```

| Step | Where |
|------|--------|
| Install / enable | [Capas](../ui/capas.md) |
| Reusable secrets | [Credentials](../ui/credentials.md) |
| Team tool ceiling | [Departments](../ui/departments.md) |
| Agent narrowing | [Agents](../ui/agents.md) |
| Approvals | [My work](../ui/my-work.md) |

User accounts: **[Users](../ui/users.md)** · **[Access & roles](../ui/access-and-roles.md)**

---

## Step-by-step integration guides

These guides walk through OAuth, scopes, and first tasks for common stacks:

| Integration | Guide | Typical capa |
|-------------|-------|--------------|
| Microsoft 365 | [MICROSOFT365](../../MICROSOFT365) | `microsoft365` |
| Google Workspace | [GOOGLE_WORKSPACE](../../GOOGLE_WORKSPACE) | `google_workspace` |
| Example helpdesk team | [HELPDESK_AGENT](../../HELPDESK_AGENT) | `helpdesk_support_agent` |

More vendors follow the same pattern: install the capa, complete setup, allow
tools on the department, run a low-risk task first.

---

## Related

- [Key concepts — Capa](../key-concepts.mdx#capa) — glossary entry
- [How work moves](../ui/how-work-moves.md) — where Capas sit in day-to-day work
- [Developer: capa types](../../developer/plugin-types.md) — manifest and code layout
- [Capa folder reference](https://github.com/oc8/oc8/blob/main/capas/README.md) — exhaustive on-disk reference
