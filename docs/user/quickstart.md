# Quickstart — install and run oc8

Get a working oc8 instance on **your own machine or server** in about 10–15
minutes. No oc8.cloud account — you host everything yourself.

## What you need

| Requirement | Notes |
|-------------|-------|
| **Docker** + Compose v2 | `docker compose version` must work |
| **4 GB+ free RAM** | More if you run Ollama locally |
| **Git** | To clone the repository |
| **Network** | Images pull from public registries on first start |

Alternative: **Podman** instead of Docker — see
[Deploy § Podman](../../DEPLOY#running-on-podman-instead-of-docker).

Windows: use PowerShell and `.\scripts\quickstart.ps1` instead of the bash
script below.

---

## Step 1 — Get the code

```bash
git clone https://github.com/oc8/oc8.git
cd oc8
```

Or download and unpack a release tarball from GitHub if you prefer not to use git.

---

## Step 2 — Start the stack

From the repository root:

```bash
./scripts/quickstart.sh
```

The script will:

1. Check that Docker (or Podman) is running
2. Create `.env` from `.env.example` if missing
3. Ask which **mode** to run:
   - **Community** (default) — empty instance, real password setup — this is
     what the rest of this guide assumes
   - **Demo** — seeded bilingual ACME showcase data behind a real password
     login
   - **Dev** — seeded ACME data with an unauthenticated instant login;
     localhost only, never expose this
4. For Community/Demo, ask whether you already run your own reverse proxy on
   this host (nginx, Traefik, another Caddy):
   - **Yes** — oc8's own Caddy binds to `127.0.0.1` on a plain port instead
     of `:80`/`:443` and never tries to obtain a certificate; you point your
     proxy at that port and it owns the domain/TLS
   - **No** (default) — offers a custom domain for automatic HTTPS instead
     (good for a hosted walkthrough, e.g. `demo.yourcompany.com` — point DNS
     at the host first)
5. Generate secrets (`OC8_JWT_SECRET`, `OC8_SECRET_KEK`, `POSTGRES_PASSWORD`,
   and for Demo mode, the demo login password)
6. Build and start all services (Postgres, Redis, API, workers, frontend, Caddy)
7. Wait until `/health` responds
8. Print the URL to open in your browser (usually `http://localhost/`, your
   custom domain, or a custom port if 80 is taken)

Running from a script or CI (no terminal attached) skips the prompts and
defaults to Community mode with no proxy and no domain — set
`OC8_QUICKSTART_MODE` (`community`/`demo`/`dev`), `OC8_QUICKSTART_OWN_PROXY`
(`y`/`n`, plus `OC8_QUICKSTART_PROXY_PORT`/`OC8_QUICKSTART_EXTERNAL_URL` when
`y`), and `OC8_QUICKSTART_DOMAIN` beforehand to choose without being asked.

**Manual alternative** (same result, more control):

```bash
cp .env.example .env
# Set the three required secrets — see DEPLOY.md
docker compose up -d --build
```

→ Full details: [Deploy](../../DEPLOY)

---

## Step 3 — First login and welcome wizard

1. Open the URL printed by the script.
2. Complete the **administrator setup wizard** — organisation, first department,
   first agent, **model**, first tool, guardrails.
3. When you finish, you land in **[Office](ui/office.md)**.

The wizard is the normal path. You do **not** need to edit `.env` or run extra
Docker commands for a first try — configure the model in **Settings → Models**
when the wizard asks (or right after in the UI).

→ Screen-by-screen: [Welcome wizard](ui/welcome.md)

On a fresh demo install with `docker-compose.dev.yml`, dev-login may skip the
wizard and show seeded demo data. Use that **only on localhost**, never on a
network-reachable host.

---

## Step 4 — Run your first task

If you completed the wizard, you already have a department, agent, and model.

1. Open **[Office](ui/office.md)** or go to your agent.
2. Click **Run** (or use chat) and describe one clear outcome.
3. Watch the run in **Live Log** or **[Activity](ui/activity.md)**.
4. If something needs a human, check **[My work](ui/my-work.md)** — not a
   separate Approvals menu.

---

## Optional — finish setup in the UI later

Skipped a wizard step? Use the sidebar — no terminal required:

| You still need… | Where in the UI |
|-----------------|-----------------|
| A model | [Settings → Models](ui/models.md) |
| Tools / integrations | [Capas](ui/capas.md) |
| Team boundaries | [Departments](ui/departments.md) |
| Another agent | [Agents](ui/agents.md) or hire from a capa template |
| Pilot checklist | [Settings → General](ui/settings-general.md) |

→ [UI guide](ui/index.md) · [How work moves](ui/how-work-moves.md)

---

## Advanced — configure models via `.env` or Ollama

Use this only if you **prefer** provider keys in the host environment, or you
run Ollama as a Compose profile. The UI wizard and **Settings → Models** work
without these steps.

### Cloud provider key in `.env`

```bash
# One of:
OC8_ANTHROPIC_API_KEY=sk-ant-...
OC8_OPENAI_API_KEY=sk-...
```

```bash
docker compose up -d
```

Then in the UI: **Settings → Models** — pick the provider you configured.

### Local Ollama (no cloud API key)

```bash
docker compose --profile ollama up -d
docker compose exec ollama ollama pull mistral
```

In **Settings → Models**, select the Ollama model you pulled.

---

## Verify the stack (optional)

```bash
curl -s http://localhost/health
# -> {"status":"ok",...}

docker compose ps
# migrate = exited(0); backend, worker, frontend, caddy = running
```

---

## Before you expose this to the internet

The default stack is for **local evaluation**. Before anyone else can reach it:

| Must do | Why |
|---------|-----|
| Set `OC8_ENV=prod` | Disables unauthenticated dev-login |
| Configure real login | Wizard / local accounts — not dev-login on a public URL |
| Use TLS (reverse proxy) | Default is HTTP only |
| Rehearse backup/restore | [BACKUP_RESTORE](../BACKUP_RESTORE.md) |
| Read scope & limits | [SCOPE_AND_LIMITATIONS](../SCOPE_AND_LIMITATIONS.md) |

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Script says Docker not running | Start Docker Desktop / `systemctl start docker` |
| Port 80 in use | Set `OC8_HTTP_PORT=8090` in `.env`, re-run `docker compose up -d` |
| Run stays `queued` | Check worker: `docker compose ps worker` — restart if needed |
| Model errors | Add or fix provider in **Settings → Models** (or wizard); for `.env`/Ollama see [Advanced](#advanced--configure-models-via-env-or-ollama) |
| Blank page after start | Wait for migrate to finish: `docker compose logs migrate` |

---

## What's next

| Goal | Document |
|------|----------|
| Understand vocabulary | [Key concepts](key-concepts.mdx) |
| Day-to-day operator work | [Daily workflow](daily-workflow.md) |
| Approvals and autonomy | [Governance and approvals](governance-and-approvals.md) |
| Deploy on a server | [Install and maintain](install-and-maintain/index.md) |
| Build an integration | [Developer: first capa](../developer/tutorial-first-plugin.md) |
