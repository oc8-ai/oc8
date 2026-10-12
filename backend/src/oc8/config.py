"""Application configuration, loaded from environment / .env (prefix OC8_)."""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OC8_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "dev"

    # Demo mode: load the full ACME showcase seed (agents, departments, tasks,
    # approvals, bilingual EN/DE presentation) so a fresh stack can be walked
    # through as a product demo. Prefer OC8_ENV=prod + compose.demo.yml for a
    # hosted walkthrough with real password login; compose.dev.yml still pairs
    # demo with unauthenticated dev-login for local hacking only.
    demo: bool = False
    # Sign-in for the seeded ACME demo admin (only used when demo=True). Empty
    # password refuses to seed the member -- never invent a default secret that
    # could ship onto a reachable host by accident.
    demo_email: str = "demo@oc8.ai"
    demo_password: str = ""

    # Runtime DB connection (RLS-bound app role, async driver).
    database_url: str = "postgresql+asyncpg://oc8_app:oc8@localhost:5433/oc8"
    # Migration DB connection (schema owner, sync driver for Alembic).
    migration_url: str = "postgresql+psycopg://oc8_migrate:oc8@localhost:5433/oc8"

    redis_url: str = "redis://localhost:6381/0"

    # Plugin discovery (§13): directories scanned for plugin folders, each
    # containing a plugin.toml. Comma-separated for multiple roots, like Odoo's
    # addons_path. Operator configuration -- never taken from a request.
    capas_path: str = "capas"

    # Core UI translations: directory holding one `<locale>.po` per language
    # (oc8.i18n.catalog). Same convention as capas_path -- a relative path
    # resolved against the working directory, mounted read-only so dropping
    # in a new file needs no image rebuild, just a backend restart.
    core_i18n_path: str = "i18n"

    # Max tool-using steps one agent run may take before it stops (§8.3). The
    # framework default; an operator may override it per agent, in either
    # direction, via agent.definition["max_steps"] (see AssignedModelPanel /
    # PATCH /agents/{id}/model-config). High rather than low by default: a
    # run that stops mid-task because it hit an arbitrary step ceiling looks
    # identical to one that finished, which is worse than a slower run.
    # Token/budget metering (oc8.metering.check_budget) is the real cost
    # backstop -- it is just checked once per run, not per step, so this is
    # deliberately a large finite number, not literal infinity.
    agent_max_steps: int = 200

    # Isolated agent runtime (§8.5): the control-plane internal base URL an
    # isolated agent container calls back on (compose service name by default),
    # the runtime image, and a flag to route ALL runs through the isolated
    # Docker runtime (per-agent container) rather than in-process.
    internal_base_url: str = "http://backend:8099"
    agent_runtime_image: str = "oc8-backend:latest"
    claude_code_agent_image: str = "oc8-agent-claude-code:latest"
    codex_agent_image: str = "oc8-agent-codex:latest"
    opencode_agent_image: str = "oc8-agent-opencode:latest"
    agent_isolation: bool = True
    # oc8_agents, NOT oc8_default: agent containers must land on their own
    # isolated network, which cannot reach the database or the internet.
    # oc8_default is the network the CONTROL PLANE (and postgres) sit on --
    # putting an agent container there defeats the isolation this whole
    # runtime exists for. Compose always injects OC8_AGENT_RUNTIME_NETWORK, so
    # this default only bites a bare `uvicorn`/pytest run with no env at all;
    # it must fail toward the safe network, not the reachable one.
    agent_runtime_network: str = "oc8_agents"

    # Which deployment owns the containers this process creates, stamped as the
    # `oc8.namespace` label (see oc8.sandbox.docker_driver). Two oc8 stacks
    # sharing one Docker host -- dev next to staging, or two self-hosted
    # installs on the same box -- would otherwise use the identical
    # `oc8.sandbox=1` label, and the startup reaper of one could remove live
    # containers belonging to the other. "default" only bites a single-stack
    # deployment, which cannot collide with itself.
    deployment_namespace: str = "default"

    # Container-runtime adapter (§8.5-family): the host-identical scratch root
    # a container runtime uses for per-run state (e.g. per-run session
    # folders), bind-mounted into the control plane at its OWN absolute path,
    # because a sibling container's bind mounts are resolved by the docker
    # daemon on the host, not inside this process's container. Which runtime
    # and what it stores here is a plugin concern -- core only owns the path
    # both sides must agree on.
    runtime_session_root: str = "/var/lib/oc8/sessions"

    # Evidence retention (§12.5.1). A run's session folder is what answers WHY
    # an agent did something; today nothing chains, retains or prunes it, and it
    # is three orders of magnitude bigger than the ledger. See oc8.evidence.
    evidence_sweep_enabled: bool = False
    """Archive finished runs' evidence and reduce it when its window closes.

    Off by default because this job DELETES files. Turning it on is lossless on
    its own -- the loose tree is replaced by an archive holding the same bytes,
    whose hash goes into the audit chain before anything is removed. Only
    `evidence_retention_days` makes it lossy, and that is separately opt-in."""

    evidence_archive_root: str = ""
    """Where archives are kept. Empty means `<runtime_session_root>/archive`.

    Separate from the session root so a deployment can put evidence on cheaper,
    slower, or WORM storage without moving the scratch space a live run writes
    into -- these have opposite access patterns and opposite lifetimes."""

    evidence_archive_after_minutes: int = 60
    """How long a run stays terminal before its evidence is archived.

    Not zero: the same window the container reaper uses, for the same reason --
    a resumed leg, a log read, or an operator looking at a just-failed run must
    not race the sweep."""

    evidence_retention_days: int = 0
    """How long an archive is kept before being reduced to its ledger entry.

    0 means never reduce: evidence accumulates, in archived form, until an
    operator chooses a window. §12.5.1's rule is that retention is REDUCTION,
    not deletion -- the ledger's record of what happened (and this archive's
    hash) survives the full retention period; only the ability to re-read the
    reasoning expires. Silently dropping it would turn an auditor's question
    into an unanswerable one with nobody able to say when."""

    # Knowledge deletion reconciliation (P2-7). A document that vanishes upstream
    # stays answerable for ever today: the sync only ever adds. These settings
    # govern the one path that can conclude "gone" from a connector's listing --
    # a machine's inference, so every default here keeps the content. See
    # oc8.knowledge.reconcile. (Superseding a re-ingested document has no flag:
    # it is reversible for the grace window, and the duplicate it prevents is a
    # live defect, not a new capability.)
    knowledge_reconcile_enabled: bool = False
    """Let an attested listing mark a vanished document, and the sweep kill it.

    Off by default because this job DELETES customer knowledge, and unlike the
    evidence sweep it is not lossless even when it works: an authoritative
    listing is the only witness that a document is gone, and a revoked token
    looks exactly like an emptied folder. The flag gates the observation too,
    not only the sweep -- a deployment that cannot act on a mark should not be
    writing marks and held-state into an append-only ledger either."""

    knowledge_missing_confirm_hours: int = 72
    """How long a document must stay absent, across two attested syncs, to die.

    Two daily cycles plus a weekend. The separation is the point, not the count:
    two syncs a minute apart during a one-minute permissions blip are not two
    observations that agree. Lowering it trades the undo window against how
    fast an upstream deletion stops being answerable."""

    knowledge_vanish_ratio_limit: float = 0.5
    """Fraction of a source's documents that may newly vanish in one listing.

    Above it the source is held and nothing is marked until a human looks. A
    credential that expired, a bucket policy that was revoked and a folder id
    that no longer resolves all present as "most of it is gone at once"; a
    customer who really did delete everything has DELETE /knowledge/sources."""

    knowledge_vanish_min_documents: int = 3
    """Floor below which the ratio above is not consulted at all.

    Ratios behave badly at small N: a two-document source losing its second
    is not evidence of a credential failure, and holding it would make the
    smallest sources the hardest to keep in sync."""

    knowledge_reduce_after_hours: int = 168
    """How long a tombstone keeps its content before the sweep destroys it.

    7 days: the undo window for an inference. §12.5.1's rule that retention is
    REDUCTION applies here too -- the chunk row and the ledger's digest of what
    was destroyed survive; only the ability to re-read the text expires, and
    until it does one route puts the document back. Operator deletes do not
    wait: a human named the object, and that is the GDPR path."""

    # Runtime-provisioner (Community privilege separation): which SandboxDriver
    # implementation `oc8.sandbox.get_sandbox_driver()` hands back. "docker"
    # (default) talks to the local Docker socket in-process, same as always.
    # "provisioner" routes every sandbox operation over HTTP to the separate
    # runtime-provisioner service instead -- the point being that the main
    # backend/worker processes then need NO docker.sock mount at all; only the
    # narrow provisioner process does. See oc8.sandbox.provisioner_driver and
    # oc8.runtime_provisioner.app.
    sandbox_driver: str = "docker"
    sandbox_provisioner_url: str = "http://runtime-provisioner:8090"
    sandbox_provisioner_token: str = ""

    # Shared secret, das die eigenen service-to-service Tenant-Provisioning-
    # Route(n) dieser Instanz in `Authorization: Bearer <token>` erwarten.
    # Wird von oc8-enterprises admin/tenants-Router befüllt (Community definiert
    # das Feld, damit jede Edition dieselbe Settings-Klasse teilt); leer bedeutet,
    # dass die Route unerreichbar ist (require_service_token liefert immer 401).
    tenant_provisioning_token: str = ""

    sandbox_user: str = ""
    """``uid:gid`` for sandbox containers, empty to keep the image's user.

    Set it to the uid that owns the runtime's scratch directories on the host --
    on Linux the container cannot otherwise read a 0600 file the control plane
    wrote. Empty by default because the right value is a property of the
    deployment, not of the code. Validated below (``_validate_sandbox_user``):
    empty, ``uid``, or ``uid:gid``, both integers -- anything else fails at
    startup rather than mid-run."""

    jwt_secret: str = "dev-secret-change-me-0123456789abcdef"
    jwt_alg: str = "HS256"
    jwt_ttl_seconds: int = 3600

    # Web Push (VAPID key pair). One pair per instance, not a tenant secret.
    # Empty keys disable push sending (checked at send time, not startup).
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = "mailto:admin@example.com"

    cors_origins: str = "http://localhost:8080,http://localhost:8081,http://localhost:8082"

    # Model Router
    ollama_base_url: str = "http://localhost:11434"
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    mistral_api_key: str = ""
    mistral_base_url: str = "https://api.mistral.ai/v1"
    default_model_provider: str = "ollama"
    default_model: str = "mistral:latest"
    default_embedding_model: str = "nomic-embed-text"

    # BYOK (§9): when True, a cloud completion with no per-tenant key is refused
    # rather than silently charged to the platform key. Off by default so dev and
    # an in-progress pilot keep working on the platform key.
    require_tenant_model_key: bool = False

    github_webhook_secret: str = ""

    # Object storage (file attachments: chat + agent Instructions uploads).
    # Bundled-by-default, switchable via env: dev/self-hosted points at the
    # bundled MinIO container; swap these to any S3-compatible endpoint
    # (AWS S3, R2, etc.) for production without a code change. See
    # oc8.storage.s3.
    s3_endpoint: str = "http://minio:9000"
    s3_access_key: str = "oc8-minio"
    s3_secret_key: str = ""
    s3_bucket: str = "oc8-uploads"
    s3_region: str = "us-east-1"

    log_sql: bool = Field(default=False)
    #: Root log level for every oc8 process. INFO by default because the level
    #: only matters once something is listening at all, and until recently
    #: nothing was: no handler was ever attached, so every warning this codebase
    #: emitted was discarded. See oc8.observability.logs.
    log_level: str = Field(default="INFO")

    # Secret store (§12.3): base64-encoded 256-bit root KEK for envelope encryption.
    secret_kek: str = ""

    #: Model prices are global (oc8.api.v1.model_prices): on a deployment
    #: shared by several tenants, one tenant's admin would change every
    #: tenant's prices. True keeps the reads and refuses tenant writes; the
    #: operator maintains prices outside the tenant API.
    model_prices_read_only: bool = False

    # Third-party OAuth (§11.2). Tenant-supplied credentials take precedence;
    # these are the platform-wide fallback (design decision 2).
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    # Externally reachable base URL of THIS api — must match the redirect URI
    # registered with the provider.
    oauth_redirect_base_url: str = "http://localhost:8000"
    # Where the callback sends the browser back to.
    frontend_base_url: str = "http://localhost:8080"

    # Audit chain MAC (§12.5 hardening): when enabled, audit_event rows are
    # hashed with HMAC-SHA256 under a key derived from secret_kek instead of a
    # bare sha256, so database access alone no longer suffices to forge
    # history. ENABLING THIS IS A ONE-WAY DOOR for a deployment: once a tenant
    # has a keyed row, append_event refuses to write an unkeyed one, so rolling
    # back to a build without this setting stops all writes. See the design
    # spec's Risks section.
    audit_mac_enabled: bool = False

    # Observability (§18). Off by default => fully inert (no provider, no exporter).
    otel_enabled: bool = False
    otel_exporter: str = "otlp"  # otlp | console | none
    otel_exporter_otlp_endpoint: str = ""
    otel_service_name: str = "oc8"
    otel_metrics_enabled: bool = True

    @model_validator(mode="after")
    def _validate_sandbox_user(self) -> Settings:
        # A malformed value here would otherwise surface an hour later as a
        # raw ValueError mid-run (chowning a session dir, or worse, an int()
        # crash inside the sandbox driver) -- disconnected from the typo that
        # caused it. Same principle as _validate_audit_mac: an operator's
        # config mistake stops the process at startup, not a live agent run.
        if self.sandbox_user:
            parts = self.sandbox_user.split(":")
            if len(parts) not in (1, 2) or not all(p.isdigit() for p in parts):
                raise ValueError(
                    "sandbox_user must be empty, 'uid', or 'uid:gid' with integer "
                    f"parts, got {self.sandbox_user!r}"
                )
        return self

    @model_validator(mode="after")
    def _validate_sandbox_provisioner(self) -> Settings:
        # Same "fail at startup, not mid-run" principle as _validate_sandbox_user:
        # a missing token or a malformed URL would otherwise only surface the
        # first time an agent run tries to provision a sandbox.
        if self.sandbox_driver == "provisioner":
            if not self.sandbox_provisioner_token:
                raise ValueError(
                    "sandbox_driver='provisioner' requires sandbox_provisioner_token to be set"
                )
            parsed = urlparse(self.sandbox_provisioner_url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError(
                    "sandbox_driver='provisioner' requires sandbox_provisioner_url to be an "
                    f"absolute http(s) URL, got {self.sandbox_provisioner_url!r}"
                )
        return self

    @model_validator(mode="after")
    def _validate_audit_mac(self) -> Settings:
        if self.audit_mac_enabled:
            # Deferred import: keyprovider imports get_settings from this
            # module, so a module-level import here would be circular.
            from oc8.secrets.keyprovider import validate_kek

            try:
                validate_kek(self.secret_kek)
            except Exception as exc:
                raise ValueError(
                    f"audit_mac_enabled=True requires a valid secret_kek: {exc}"
                ) from exc
        return self

    @model_validator(mode="after")
    def _validate_otel(self) -> Settings:
        if (
            self.otel_enabled
            and self.otel_exporter == "otlp"
            and not self.otel_exporter_otlp_endpoint
        ):
            raise ValueError(
                "otel_enabled with otel_exporter='otlp' requires otel_exporter_otlp_endpoint"
            )
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def capas_path_list(self) -> list[str]:
        return [p.strip() for p in self.capas_path.split(",") if p.strip()]

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"

    @property
    def is_demo(self) -> bool:
        return self.demo

    @property
    def migration_async_url(self) -> str:
        """Owner role over the async driver — used by the seeder (bypasses RLS)."""
        return self.migration_url.replace("+psycopg", "+asyncpg")


@lru_cache
def get_settings() -> Settings:
    return Settings()
