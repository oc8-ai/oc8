# Deploying oc8 with Docker Compose

A single `docker compose up` brings up the whole stack — Postgres/pgvector,
Redis, the backend API plus its worker/ingestion-worker/scheduler, the built
frontend, and a Caddy reverse proxy that is the one external origin — for
**testing on a server**. It is not a production deployment; see *Deliberate
limits* at the end.

> **Kubernetes?** See [Helm chart](../deploy/helm/oc8/README.md) for the same stack
> on a cluster (`deploy/helm/oc8/`).

## Prerequisites

- A host with **Docker** and **Compose v2** (`docker compose version`) --
  or **Podman** (v4+, with the `podman compose` plugin) as a drop-in
  alternative, see below.
- **Network egress** from the host: the agent sandbox pulls its container image
  at run time, and the images build from public registries.
- For real agent runs the backend and worker mount the host's container-runtime
  socket (`/var/run/docker.sock` by default), which is **root-equivalent** —
  use a dedicated or disposable host, not one you share with anything you care
  about.

### Running on Podman instead of Docker

`scripts/quickstart.sh`/`.ps1` accept `OC8_CONTAINER_RUNTIME=podman` (default
is `docker`) to orchestrate the stack with `podman compose` instead of
`docker compose`, and to point the `backend`/`worker` socket mount at Podman's
API socket instead of Docker's -- everything else about the stack, including
the sandbox driver code, is unchanged (see the Podman section below).

Enable Podman's API socket first:

- macOS: `podman machine init && podman machine start` (the socket comes up
  automatically with the machine).
- Linux: `systemctl --user enable --now podman.socket`.

Then run `OC8_CONTAINER_RUNTIME=podman ./scripts/quickstart.sh` (or set
`OC8_CONTAINER_RUNTIME=podman` once in `.env` to make it the default for this
checkout). The script fills in `OC8_CONTAINER_SOCKET` for you.

One security difference worth knowing: **rootless Podman's socket is scoped
to the invoking user's own containers**, not root-equivalent the way the
Docker daemon socket is -- so the warning above applies to Docker and to
rootful Podman, but not to rootless Podman.

For local frontend-only work, run commands from the frontend workspace:

```bash
cd frontend
npm run lint
npm run build
```

## First bring-up

```bash
cp .env.example .env

# Fill the three required secrets:
sed -i "s|^OC8_JWT_SECRET=.*|OC8_JWT_SECRET=$(openssl rand -hex 32)|" .env
sed -i "s|^OC8_SECRET_KEK=.*|OC8_SECRET_KEK=$(openssl rand -base64 32)|" .env
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 16)|" .env

# (macOS sed: use `sed -i ''` instead of `sed -i`.)

docker compose up -d --build
docker compose logs -f migrate     # watch it apply migrations, seed, then exit 0
```

The `migrate` service runs `alembic upgrade head` and, with the default
`.env.example` values (`OC8_ENV=prod`, `OC8_SEED_ON_START=false`), nothing
else — the instance starts empty and `POST /auth/setup` creates its first
administrator. The API, worker, ingestion-worker and scheduler all wait for
`migrate` to finish, so migration runs exactly once.

If port 80 is taken, set `OC8_HTTP_PORT` in `.env` (e.g. `8090`) and re-up.

## Verify

```bash
docker compose ps                  # migrate = exited(0); the rest running/healthy
curl -s http://<host>/health       # -> {"status":"ok","version":"..."}
```

Then open `http://<host>/` in a browser: it lands on the Community setup
screen, and creating the local administrator account proves the whole path
(frontend → relative `/api/v1` → backend → Postgres) works through the single
Caddy origin, with no CORS.

For a **hosted product walkthrough** (seeded bilingual ACME data behind a real
password login, no open `dev-login`), set `OC8_DEMO_PASSWORD` in `.env` (min 8
chars) and bring the stack up with the demo override:
`docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build`.
Sign in as `OC8_DEMO_EMAIL` (default `demo@oc8.ai`) with that password. Add a
custom domain (`OC8_DOMAIN` in `.env`) for automatic HTTPS — see the HTTPS
section below; point DNS at the host first.

For a **local hacking** stack — seeded ACME and unauthenticated `dev-login`,
never on a reachable host — use the dev override instead:
`docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build`.

`scripts/quickstart.sh`/`.ps1` wrap all three of the above (Community, Demo,
Dev) behind one interactive prompt — including the custom-domain question and
the reverse-proxy question below — instead of hand-picking `-f` files and
editing `.env`; see [Quickstart](user/quickstart.md).

## Giving agents a model

The app runs without a model, but an actual agent run needs one. Either:

- **A cloud key** — set `OC8_ANTHROPIC_API_KEY` (or `OC8_OPENAI_API_KEY` /
  `OC8_MISTRAL_API_KEY`) in `.env` and re-up; or
- **Local Ollama** —
  ```bash
  docker compose --profile ollama up -d
  docker compose exec ollama ollama pull mistral
  ```
  CPU-only Ollama is slow but works for testing. For a GPU host, add a
  `deploy.resources.reservations.devices` block to the `ollama` service per the
  Ollama docs.

## Kubernetes (Helm)

For clusters, use the chart in `deploy/helm/oc8/` (same components as Compose).

```bash
# Build and push/load images first — see deploy/helm/oc8/README.md
export OC8_JWT_SECRET=$(openssl rand -hex 32)
export OC8_SECRET_KEK=$(openssl rand -base64 32)
export POSTGRES_PASSWORD=$(openssl rand -hex 16)

helm upgrade --install oc8 ./deploy/helm/oc8 \
  --namespace oc8 --create-namespace \
  --set secrets.jwtSecret="$OC8_JWT_SECRET" \
  --set secrets.secretKek="$OC8_SECRET_KEK" \
  --set secrets.postgresPassword="$POSTGRES_PASSWORD"
```

Capas are not baked into the image — mount `capas/` via `backend.capas.hostPath` or a
PVC. Agent sandboxes (Docker socket) are off by default on Kubernetes.

## HTTPS

For a real domain, set `OC8_DOMAIN` in `.env` (the quickstart scripts ask for
this too) and re-up. Caddy provisions and renews a Let's Encrypt certificate
automatically. Point the domain's DNS at the host first.

### Behind your own reverse proxy

If another proxy on the host (nginx, Traefik, another Caddy) already owns
the domain and its TLS certificate, oc8's own Caddy must not also try to
bind `:80`/`:443` or provision one. Set `OC8_HTTP_PORT=127.0.0.1:<port>` and
leave `OC8_DOMAIN` empty, then point your proxy's upstream at that
`127.0.0.1:<port>` over plain HTTP. Set `OC8_FRONTEND_BASE_URL` to the
externally visible `https://...` URL your proxy serves this under, so
generated links (e.g. password-reset emails) resolve correctly.

## Enabling the audit MAC (advanced, off by default)

`OC8_AUDIT_MAC_ENABLED` keys the audit hash chain. It is **off by default** and
not part of a normal bring-up. Before enabling it, understand that this is a
one-way door: once a tenant is keyed, a rollback to a build without the setting
stops writes, and turning the flag on over an existing chain requires
`docker compose run --rm backend oc8 audit-adopt-checkpoints` followed by a full
verify.

## Pruning run evidence (off by default)

Every agent run leaves a session folder on disk — the transcript, the standing
instructions it was given, its session state. That is the *evidence* half of the
audit trail: what answers **why** an agent did something, as opposed to
the ledger, which answers **what** it did. Nothing prunes it, and it is far
larger than the ledger. Measured on the dev box: 134 MB for 406 runs, growing
with every run.

`OC8_EVIDENCE_SWEEP_ENABLED=true` turns on a sweep (worker housekeeping timer,
or `docker compose run --rm backend oc8 evidence-sweep` by hand) that, for each
finished run past `OC8_EVIDENCE_ARCHIVE_AFTER_MINUTES` (default 60):

1. packs the folder into one `.tar.xz` under `<session root>/archive/<agent>/`,
2. writes an `evidence.archived` entry into the tenant's audit hash chain
   carrying the archive's SHA-256, **and commits it**,
3. only then deletes the loose folder.

Nothing is lost: the archive holds the same bytes and is readable with nothing
but a Python standard library. What it does drop is what the container runtime
wrote for *itself* — on the dev box that was 34 % of all bytes, mostly an
undeliverable telemetry spool — and the ledger entry records how many bytes were
dropped, so it is visible rather than silent.

Measured on that same tree: **134 MB → 11 MB**, 6,273 files → 588, and the chain
still verifies (`oc8 audit-verify --full --once`).

`OC8_EVIDENCE_RETENTION_DAYS` is separate and defaults to **0 = never**. A
non-zero value is the only lossy part: when an archive is older than the window
it is replaced by an `evidence.reduced` entry naming its hash, so the proof of
what happened survives while the ability to re-read the reasoning expires. Set
it deliberately, not to save space you have.

## Operating notes

- **Logs:** `docker compose logs -f backend` (or `worker`, `scheduler`, …).
- **Backup and restore:** follow [the pilot backup/restore runbook](PILOT_BACKUP_RESTORE.md)
  and rehearse it before inviting pilot users.
- **Reset data:** `docker compose down -v` drops the Postgres volume; the next
  `up` migrates a fresh, empty instance (or re-seeds, with `OC8_SEED_ON_START=true`
  under the dev override). Omit `-v` to keep data across restarts.
- **Update to new code:** `git pull && docker compose up -d --build`. The
  `migrate` service applies any new revisions before the app restarts.
- **The four backend processes** are one image with different commands:
  `backend` (uvicorn), `worker`, `ingestion-worker`, `scheduler`. Scale or
  restart them independently with `docker compose restart <service>`.

## Deliberate limits

This stack is for testing, and skips what production needs:

- **dev-login is an unauthenticated admin bypass, and it is off by default.**
  `.env.example` ships `OC8_ENV=prod`, so `POST /api/v1/auth/dev-login` 404s
  and the only way in is real password auth (`/auth/setup` then `/auth/login`).
  Setting `OC8_ENV=dev` (or using the `docker-compose.dev.yml` override, which
  sets it for you) re-enables dev-login: it then mints an `org_admin` token for
  the seed tenant for *anyone who can reach it*, and Caddy exposes it. Only do
  that for a local demo, and never on a publicly reachable host — if you do,
  restrict it at the network layer too (firewall, VPN, or bind the published
  port to a private interface, e.g. `OC8_HTTP_PORT` published as
  `127.0.0.1:8090:80` behind your own proxy).
- **Weak internal credentials, not exposed.** The `oc8_app`/`oc8_migrate`
  database roles use the password `oc8` (fixed by `backend/docker/init-db.sql`).
  This is acceptable *only because* Postgres and Redis have **no port
  mapping** in this compose — they are reachable only on the internal Docker
  network, and Caddy is the sole exposed service. The credentials that a
  network attacker could reach — `OC8_JWT_SECRET`, `OC8_SECRET_KEK`, the
  Postgres superuser — are required (`:?`) and generated per the setup above,
  never defaulted. For production you would additionally rotate the role
  passwords.
- The Docker-socket mount on `backend`/`worker` is **root-equivalent** on the
  host.
- No forced TLS (http by default), no secrets manager (secrets live in `.env`),
  no HA, no backups, no OpenTelemetry collector (OTLP export is disabled by
  default; point `OTEL_EXPORTER_OTLP_ENDPOINT` at a collector and set
  `OTEL_SDK_DISABLED=false` to use it).
- The agent sandbox pulls its image at run time and starts sibling containers on
  the host — a standard docker-out-of-docker arrangement, fine for
  straightforward runs but not isolated the way a production runtime would be.
