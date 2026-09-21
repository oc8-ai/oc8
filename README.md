# oc8

<p align="center"><img src="frontend/public/octopus_oc8.svg" width="160" alt="oc8 Octopus" /></p>

<h3 align="center">The AI Business Orchestration</h3>

<p align="center"><strong>Become an AI-first business today.</strong></p>

<p align="center">
  oc8 is an open-source, self-hosted AI agent orchestration platform for businesses.
  It lets AI agents securely work across ERP, CRM, Microsoft 365, Odoo, Slack and other business systems through MCP,
  with human approvals, guardrails and audit trails.
</p>

<p align="center">
  <a href="https://oc8.ai">Website</a> &middot;
  <a href="https://docs.oc8.ai">Docs</a> &middot;
  <a href="https://discord.com/invite/vEYpvzXUv">Discord</a>
</p>

<p align="center">
  <a href="docs/GETTING_STARTED.md">Getting Started</a> &middot;
  <a href="ARCHITECTURE.md">Architecture</a> &middot;
  <a href="ROADMAP.md">Roadmap</a> &middot;
  <a href="docs/DEPLOY.md">Deploy</a> &middot;
  <a href="SECURITY.md">Security</a>
</p>

- 🔒 **Isolated AI Agents** — each agent runs in its own container, with
  access only to what oc8 explicitly grants it.
- 📚 **RAG Knowledge Base Management** — retrieval-augmented knowledge,
  scoped per agent or department.
- 🧠 **Works With Any LLM** — Anthropic, OpenAI, OpenRouter, local Ollama,
  or any OpenAI-compatible endpoint.
- 🛡️ **Sovereign by Design** — self-hosted; your data and credentials never
  leave infrastructure you control.
- 🏢 **Scoped and Built for Enterprise Usage** — tenant isolation, audit
  trails, and approval thresholds from day one.
- 🔌 **Works With Your Existing Stack** — oc8 orchestrates the CRM, ERP,
  and tools you already run; nothing to rip out and replace.
- 🧑‍⚖️ **Human in Control** — every consequential action waits for approval
  unless you've explicitly said otherwise.
- 🧩 **Extensible in Every Direction** — the capa system: connectors,
  skills, tool packs, and department templates as installable folders.
- 🔗 **Built on Open Protocols** — every tool call goes through the same
  governed MCP gateway, compatible with standard agent tooling.
- 📈 **Built to Scale** — from a single agent to a full digital workforce.
- 🗂️ **Knowledge & Tool Management for Agents** — assign exactly what each
  agent can read, call, and use.
- 🏙️ **Your Digital Company** — departments, agents, and approvals that
  mirror how your business actually runs.

> **Status:** oc8 is under active development. Read
> [Scope and limitations](docs/SCOPE_AND_LIMITATIONS.md) before a production
> deployment.

<p align="center"><img src="frontend/public/demo.gif" width="880" alt="oc8 UI walkthrough: the Office dashboard's live department view, the Agents list, an agent's detail page, its streaming Live Log, an approval request with full context, and the Costs dashboard" /></p>

**oc8 turns "hire someone to do this" into a configuration you can inspect,
approve, and audit.** You describe the outcome, the agent proposes how it'll
get there, and every consequential step — a write, a send, a spend past a
threshold — waits for a human unless you've explicitly said otherwise. Your
existing software landscape stays exactly where it is — oc8 orchestrates it,
it doesn't replace it.

## oc8 is for you if

- ✅ you want an AI agent to actually *do* recurring work — triage tickets,
  draft and send replies, update records in a real business system — not just
  answer questions in a chat window.
- ✅ you need to know, after the fact, exactly which agent did what, when, and
  under whose approval — not just trust that it behaved.
- ✅ you'd rather self-host on infrastructure you control than hand your
  business data and credentials to someone else's cloud.
- ✅ you want to plug in the tools you already use (a CRM, a helpdesk, a
  knowledge base) without hand-rolling an integration for each one — or
  ripping out and replacing what already works.
- ✅ "run this in an isolated container, not my agent host" is a requirement,
  not a nice-to-have.

## How it works

| Step | What happens |
| --- | --- |
| 1. Describe | State the outcome in plain language: "classify incoming tickets, draft a response, escalate anything about billing." |
| 2. Configure | Assign a model, the knowledge and tools it needs, and the autonomy/escalation boundary — or let the built-in Copilot draft the configuration for you to review. |
| 3. Run | The agent executes on a schedule, an event, or a chat message; every governed action is checked against its permissions before it happens. |
| 4. Govern | Anything over a value threshold or on an always-ask list waits for a human — from the inbox, or from Telegram/WhatsApp. |

<br/>

<div align="center">
<table>
  <tr>
    <td align="center"><strong>Works<br/>with</strong></td>
    <td align="center"><img src="frontend/public/logos/claude.svg" width="32" alt="Claude Code" /><br/><sub>Claude Code</sub></td>
    <td align="center"><img src="frontend/public/logos/codex.svg" width="32" alt="Codex" /><br/><sub>Codex</sub></td>
    <td align="center"><img src="frontend/public/logos/opencode.svg" width="32" alt="opencode" /><br/><sub>opencode</sub></td>
    <td align="center"><img src="frontend/public/logos/nanoclaw.svg" width="32" alt="nanoclaw" /><br/><sub>nanoclaw</sub></td>
    <td align="center"><img src="frontend/public/logos/bash.svg" width="32" alt="Bash" /><br/><sub>Bash</sub></td>
    <td align="center"><img src="frontend/public/logos/http.svg" width="32" alt="HTTP" /><br/><sub>HTTP / MCP</sub></td>
    <td align="center"><img src="frontend/public/logos/openrouter.svg" width="32" alt="OpenRouter" /><br/><sub>OpenRouter</sub></td>
  </tr>
</table>

<em>Any agent runtime that can be sandboxed and driven through oc8's gateway is hireable.</em>

</div>

<br/>

## What's under the hood

```
                         ┌─────────────────────────┐
                         │        caddy (edge)      │
                         └────────────┬─────────────┘
                    ┌──────────────────┴───────────────────┐
                    │                                       │
           ┌────────▼────────┐                    ┌─────────▼─────────┐
           │     frontend      │                    │      backend       │
           │  (React/Vite SPA) │◄──────REST/WS──────│  (FastAPI, API v1) │
           └────────────────────┘                    └─────────┬──────────┘
                                                                 │
                     ┌───────────────────┬───────────────────┬─┴───────────────┐
                     │                   │                   │                 │
              ┌──────▼──────┐    ┌───────▼──────┐   ┌────────▼───────┐  ┌──────▼──────┐
              │   worker      │    │  scheduler    │   │ ingestion-worker│  │  postgres    │
              │ (run executor)│    │ (cron/trigger)│   │  (RAG ingest)   │  │  + pgvector  │
              └───────┬───────┘    └───────────────┘   └─────────────────┘  └─────────────┘
                      │
                      │ isolated-runtime agents (opt-in per agent)
                      ▼
              ┌──────────────────────┐
              │  runtime-provisioner   │  the ONLY process with Docker socket access
              └──────────┬─────────────┘
                         │ one container per agent — spawned, reaped, never shared
        ┌────────────────┼────────────────┬────────────────┐
        │                │                │                │
  ┌─────▼─────┐    ┌─────▼─────┐    ┌─────▼─────┐    ┌─────▼─────┐
  │  agent A   │    │  agent B   │    │  agent C   │    │  agent N   │
  │ container  │    │ container  │    │ container  │    │ container  │
  │ own tools, │    │ own tools, │    │ own tools, │    │ own tools, │
  │ own creds  │    │ own creds  │    │ own creds  │    │ own creds  │
  └────────────┘    └────────────┘    └────────────┘    └────────────┘
```

Every isolated agent gets its own container — no shared filesystem, no shared
credentials, no visibility into another agent's tools or data. The
`runtime-provisioner` is the only process in the stack that ever touches the
Docker socket, so the backend and worker processes themselves never carry
that (host-root-equivalent) privilege.

| System | What it owns |
| --- | --- |
| **Microkernel core** | Agents, runs, approvals, memory, tools, secrets — no vendor-specific logic. |
| **Capas** | Everything Odoo-, Slack-, or vendor-specific. A connector, tool pack, or skill is a folder, not a fork. |
| **MCP tool gateway** | The one and only MCP server an agent ever sees; every external call is governed and auditable here. |
| **Postgres + RLS** | Row-level security keyed on a tenant GUC set per transaction — the enforced isolation primitive, not application-level filtering. |
| **Runtime isolation** | An agent runs in-process by default, or in its own Docker/Podman container for stronger blast-radius containment — either way, it only ever sees the knowledge, tools, and credentials oc8 explicitly grants it, never anything more. |
| **Secret store** | Envelope-encrypted credentials (AES-256-GCM, per-tenant DEK under an operator-held KEK) — never plaintext, never in the codebase. |

Full write-up, request-flow walkthrough, and design decisions: [ARCHITECTURE.md](ARCHITECTURE.md).

## What oc8 is not

| It's not... | Because... |
| --- | --- |
| A chatbot | An agent here does work — writes, sends, updates records — not just answers questions. |
| A no-code automation builder | Automations are scoped to what a governed agent can safely do, not arbitrary workflow graphs. |
| A prompt-management tool | Prompts are one input to an agent's configuration, not the product. |
| A hosted service | oc8 runs on infrastructure you control; there's no account to create with us. |
| A place to paste secrets into chat | Credentials live in an encrypted secret store the agent never reads directly — see [ARCHITECTURE.md](ARCHITECTURE.md#the-secret-store). |

## Quick start

Follow the complete [Getting Started guide](docs/GETTING_STARTED.md), or use
the guided local installer:

```bash
# Run oc8 locally (Docker)
git clone https://github.com/oc8-ai/oc8.git
cd oc8
./scripts/quickstart.sh          # macOS / Linux
# PowerShell on Windows:
./scripts/quickstart.ps1
```

The scripts check Docker, ask which mode to run (**Community** — empty, real
password setup; **Demo** — seeded bilingual ACME showcase behind a real
password login, good for a hosted walkthrough on a custom domain; or **Dev** —
seeded ACME data with an unauthenticated instant login, localhost only, never
expose it). For Community/Demo, they also ask whether you already run your
own reverse proxy on this host (binds oc8 to `127.0.0.1` on a plain port
instead of letting Caddy grab `:80`/`:443` or provision a certificate) or
want oc8's own Caddy to handle HTTPS on a custom domain instead. Then the
scripts create missing `.env` secrets without overwriting existing values,
build and start the stack, wait for its health endpoint, and print the URL. A
fresh Community installation opens the local administrator setup wizard. Then
configure a model, create an agent, and run a first task.

Non-interactive/scripted runs (no terminal attached) default to Community mode
with no proxy and no custom domain, same as before — set
`OC8_QUICKSTART_MODE` (`community`/`demo`/`dev`), `OC8_QUICKSTART_OWN_PROXY`
(`y`/`n`, plus `OC8_QUICKSTART_PROXY_PORT`/`OC8_QUICKSTART_EXTERNAL_URL` when
`y`), and `OC8_QUICKSTART_DOMAIN` to preset the answers instead of being
prompted.

## Your first automation

1. Create an agent and state one clear outcome under **Mission & Automation**.
2. Select its model, knowledge, and the minimum tools it needs.
3. Choose when it starts: manually, on a schedule, or from a capa event.
4. Define autonomy and escalation boundaries.
5. Test with a low-risk task, then review activity and audit events.

You can alternatively tell the OC8 Copilot what you want, for example:
"Prepare an agent that classifies support requests and escalates payment or
security cases." It will create a reviewable proposal — it never activates or
changes anything without an authorized person applying it. The Copilot
receives capabilities, not credentials: it cannot read secret values, shell
into hosts, or apply changes itself.

## Security model

- Credentials are stored in an encrypted secret store and are not exposed to
  normal agents or the OC8 Copilot.
- OAuth and secret entry happen in dedicated human-controlled flows.
- Capas are executable code and therefore a trust boundary; install only
  code you have reviewed and trust.
- The reference runtime provisioner is the only Compose service with Docker
  socket access. That access remains host-root-equivalent.

Read [SECURITY.md](SECURITY.md), [DEPLOY.md](docs/DEPLOY.md), and
[SCOPE_AND_LIMITATIONS.md](docs/SCOPE_AND_LIMITATIONS.md).

## FAQ

**Why not just give my agent framework direct API access to my tools?**
Because then nothing stops an over-threshold action from executing before a
human sees it. oc8's MCP gateway is the one path every tool call takes, which
is what makes an approval threshold enforceable instead of advisory.

**Do I need Kubernetes to run this?**
No. The reference deployment is a single `docker compose up` — Postgres,
Redis, the backend and its workers, the frontend, and a Caddy edge proxy.

**Can I add my own integration without forking the core?**
Yes — that's what the capa system is for. A data-only capa (a skill, a
department template, a tool pack's guardrail presets) needs no code at all;
see [capas/README.md](capas/README.md).

**What happens if I don't configure any approval thresholds?**
Guardrail presets shipped by a capa give you a sound default (e.g. always
requiring approval for a deletion, since it carries no monetary value to
threshold against) — you don't have to hand-assemble a policy from scratch.

## Roadmap

See [ROADMAP.md](ROADMAP.md) for what's shipped and what's next — currently:
core agent/department/approval model, provider-neutral model routing,
knowledge and memory with budgets, the capa framework, the MCP tool
gateway, guardrail presets, Docker/Podman runtime isolation, an optional
HMAC-chained audit trail, and encrypted backup/restore are all shipped;
sandboxed execution for community-trust capas is next.

## Repository map

| Path | Purpose |
| --- | --- |
| `backend/` | API, workers, data model, agent runtime, migrations, tests |
| `frontend/` | React/TanStack operator experience |
| `capas/` | Open connector, skill, channel, and runtime extensions |
| `skills-library/` | Skills for external AI clients (Claude Desktop, claude.ai, ChatGPT) that operate oc8 over `/mcp/external` |
| `docs/` | Deployment, operational, security, and getting-started guides |
| `docker-compose.yml` | Reference deployment |

## Development and verification

```bash
cd backend
python -m pytest
python -m ruff check src tests

cd ../frontend
npm run lint
npm run test:unit
npm run build
```

For a clean-install Compose smoke test:

```bash
./scripts/community-compose-fresh-install-smoke.sh
```

## Documentation

Structured like [Odoo's documentation](https://www.odoo.com/documentation/19.0/):

- [Documentation hub](docs/index.md)
- [User documentation](docs/user/index.md) — run and operate oc8
- [Install and maintain](docs/user/install-and-maintain/index.md)
- [Developer documentation](docs/developer/index.md) — build capas
- [Contributing](docs/contributing/index.md) — work on oc8 core

Reference:

- [Getting Started](docs/GETTING_STARTED.md)
- [Architecture](ARCHITECTURE.md)
- [Roadmap](ROADMAP.md)
- [Deployment guide](docs/DEPLOY.md)
- [Scope and limitations](docs/SCOPE_AND_LIMITATIONS.md)
- [Backup and restore](docs/BACKUP_RESTORE.md)
- [Capa guide](capas/README.md)
- [External Copilot skill (MCP)](skills-library/README.md)
- [Security policy](SECURITY.md)
- [Contributing](CONTRIBUTING.md)

## About

oc8 comes out of the [BOP Alliance](https://oc8.ai/about), a network of
software partners spanning thousands of projects across dozens of
countries, after years spent connecting business systems by hand. An
agent in oc8 isn't tied to one application — it reaches the ERP, the CRM,
the file store, and the accounting system in the same run, because they
all speak one open protocol. Open by default, sovereign by design, human
in control, built together with the community rather than a vendor
roadmap decided behind closed doors.

## Contributing and license

Please read [CONTRIBUTING.md](CONTRIBUTING.md),
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), and [SECURITY.md](SECURITY.md).

oc8 Community (`backend/` and `frontend/`) is licensed under the **GNU Lesser
General Public License v3.0 or later** (`LGPL-3.0-or-later`), see
[LICENSE](LICENSE). A third-party dependency audit is
recorded in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md). Each capa
under `capas/` also has its own license and provenance; see
[capas/README.md](capas/README.md). Contributions are accepted under
the [Developer Certificate of Origin](https://developercertificate.org/) —
see [CONTRIBUTING.md](CONTRIBUTING.md).
