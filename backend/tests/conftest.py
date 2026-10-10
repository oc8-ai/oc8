"""Shared test fixtures: a migrated Postgres with the two-role RLS model, an
app-bound session factory, and a Redis container. Requires Docker."""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from oc8.constants import ACME_TENANT_ID
from oc8.db.engine import dispose_engine

# Docker Desktop for Mac fails to bind-mount the docker socket for testcontainers'
# Ryuk reaper (containers "operation not supported" on /host_mnt/.../docker.sock).
# Disabling Ryuk is the documented workaround; containers are still torn down by
# the `with` blocks / context managers below on both success and failure. Scoped to
# macOS only, since Linux/CI hosts don't have this quirk and should keep Ryuk cleanup.
if sys.platform == "darwin":
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

# Bootstrap SQL mirroring docker/init-db.sql, adapted for the throwaway container.
_ROLE_BOOTSTRAP = """
CREATE EXTENSION IF NOT EXISTS vector;
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'oc8_migrate') THEN
    CREATE ROLE oc8_migrate LOGIN PASSWORD 'oc8' NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'oc8_app') THEN
    CREATE ROLE oc8_app LOGIN PASSWORD 'oc8' NOBYPASSRLS;
  END IF;
END $$;
ALTER SCHEMA public OWNER TO oc8_migrate;
GRANT USAGE ON SCHEMA public TO oc8_app;
ALTER DEFAULT PRIVILEGES FOR ROLE oc8_migrate IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO oc8_app;
ALTER DEFAULT PRIVILEGES FOR ROLE oc8_migrate IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO oc8_app;
"""


def _urls(container: PostgresContainer) -> tuple[str, str, str]:
    """(superuser_sync, migrate_async, app_async) URLs for the container's db."""
    host = container.get_container_host_ip()
    port = container.get_exposed_port(5432)
    su = container.username
    pw = container.password
    db = container.dbname
    superuser = f"postgresql+psycopg://{su}:{pw}@{host}:{port}/{db}"
    migrate = f"postgresql+asyncpg://oc8_migrate:oc8@{host}:{port}/{db}"
    app = f"postgresql+asyncpg://oc8_app:oc8@{host}:{port}/{db}"
    return superuser, migrate, app


@pytest.fixture(scope="session")
def _pg() -> Iterator[PostgresContainer]:
    with PostgresContainer("opaas/postgres-vector:15", dbname="oc8") as pg:
        yield pg


@pytest.fixture(scope="session")
def pg_url(_pg: PostgresContainer) -> str:
    superuser, _migrate, _app = _urls(_pg)
    return superuser


@pytest.fixture(scope="session", autouse=True)
def settings_env(_pg: PostgresContainer) -> Iterator[None]:
    """Point oc8 at the container, bootstrap roles, run migrations."""
    import psycopg

    superuser, migrate_url, app_url = _urls(_pg)
    # 1. Bootstrap the two roles as superuser (autocommit for CREATE ROLE).
    with psycopg.connect(superuser.replace("+psycopg", ""), autocommit=True) as conn:
        conn.execute(_ROLE_BOOTSTRAP)

    # 2. Point config at the container and run alembic upgrade head.
    os.environ["OC8_MIGRATION_URL"] = migrate_url.replace("+asyncpg", "+psycopg")
    os.environ["OC8_DATABASE_URL"] = app_url
    from oc8.config import get_settings

    get_settings.cache_clear()

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests() -> AsyncIterator[None]:
    """`execute_run` (and future runtime tasks) bind the tenant via the
    module-global engine in `oc8.db.engine`, which pytest-asyncio's per-test
    event loop would otherwise reuse across loops. Dispose it after each test
    so the next test rebuilds it against its own loop. Disposing a
    never-created engine is a no-op, so this is safe for every test."""
    yield
    await dispose_engine()


AppSessionFactory = Callable[[uuid.UUID], AbstractAsyncContextManager[AsyncSession]]


@pytest.fixture
def app_session() -> AppSessionFactory:
    """Factory: async context manager yielding an RLS-bound app AsyncSession."""

    @asynccontextmanager
    async def _open(tenant_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
        engine = create_async_engine(os.environ["OC8_DATABASE_URL"], poolclass=NullPool)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as s:
                await s.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"),
                    {"t": str(tenant_id)},
                )
                try:
                    yield s
                    await s.commit()
                except Exception:
                    await s.rollback()
                    raise
        finally:
            await engine.dispose()

    return _open


@pytest.fixture(autouse=True)
def _reset_realtime_bus() -> Iterator[None]:
    """`oc8.realtime.bus.get_event_bus()` is a lazy module-global singleton that
    captures `get_settings().redis_url` at first call. In the full suite a
    NON-realtime test (which drives a wired emit site but does NOT request the
    `redis_url` fixture) can create that singleton against the compose default
    (6381) before `redis_url` points settings at the testcontainer -- poisoning
    every later realtime emit test (subscriber on the testcontainer, publisher on
    the stale compose client). Reset the singleton around every test so it is
    rebuilt from the settings active in THAT test (testcontainer for realtime
    tests, which request `redis_url`). Production is unaffected: one process, one
    stable settings object."""
    import oc8.realtime.bus as bus_mod

    bus_mod._bus = None
    yield
    bus_mod._bus = None


@pytest.fixture(autouse=True)
def _reset_queue_singletons() -> Iterator[None]:
    """Same hazard as `_reset_realtime_bus`: `get_run_queue()` and
    `get_ingestion_queue()` are lazy module-global singletons that capture a
    redis client bound to the event loop (and settings) of first use. Across the
    full suite a queue-driving test that does NOT request `redis_url` can create
    the singleton against a stale client / a since-closed event loop, breaking a
    later test with `RuntimeError: Event loop is closed`. Reset both around every
    test so each rebuilds from the settings/loop active in THAT test. Production
    is one process with a stable loop, so it is unaffected."""
    import oc8.knowledge.worker as ingest_mod
    import oc8.runtime.queue as queue_mod

    queue_mod._queue = None
    ingest_mod._queue = None
    yield
    queue_mod._queue = None
    ingest_mod._queue = None


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    from oc8.config import get_settings

    with RedisContainer("redis:7-alpine") as rc:
        host = rc.get_container_host_ip()
        port = rc.get_exposed_port(6379)
        url = f"redis://{host}:{port}/0"
        os.environ["OC8_REDIS_URL"] = url
        get_settings.cache_clear()
        yield url


@pytest.fixture(scope="session")
def minio_url() -> Iterator[str]:
    """Session-scoped MinIO container backing `oc8.storage.s3` in tests.

    testcontainers ships no first-class MinIO container usable here:
    `testcontainers.community.minio.MinioContainer` needs the separate `minio`
    SDK, which oc8 does not depend on (oc8.storage.s3 talks to S3-compatible
    storage purely through boto3) -- so this runs the same public MinIO
    image docker-compose.yml uses, via the low-level `DockerContainer`, the
    same primitive `redis_url`/`_pg` build on above.

    Creates the test bucket and points `get_settings()` at the container for
    the whole session (same pattern as `redis_url`), then clears
    `oc8.storage.s3._client`'s lru_cache so a fresh boto3 client binds to it
    -- both before first use and again on teardown, so a later test session's
    cached client can never point at this (by then stopped) container.
    """
    import boto3

    from oc8.config import get_settings
    from oc8.storage import s3 as s3_mod

    access_key = "oc8-test"
    secret_key = "oc8-test-secret"
    bucket = "oc8-test-bucket"

    container = (
        DockerContainer("cgr.dev/chainguard/minio:latest")
        .with_exposed_ports(9000)
        .with_env("MINIO_ROOT_USER", access_key)
        .with_env("MINIO_ROOT_PASSWORD", secret_key)
        .with_command("server /data")
        .waiting_for(LogMessageWaitStrategy("API:"))
    )
    with container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(9000)
        endpoint = f"http://{host}:{port}"

        os.environ["OC8_S3_ENDPOINT"] = endpoint
        os.environ["OC8_S3_ACCESS_KEY"] = access_key
        os.environ["OC8_S3_SECRET_KEY"] = secret_key
        os.environ["OC8_S3_BUCKET"] = bucket
        os.environ["OC8_S3_REGION"] = "us-east-1"
        get_settings.cache_clear()
        s3_mod._client.cache_clear()

        # Equivalent of docker-compose's one-shot `minio-init` service: create
        # the bucket the tests write into.
        boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="us-east-1",
        ).create_bucket(Bucket=bucket)

        yield endpoint
        s3_mod._client.cache_clear()


@pytest.fixture(scope="session")
def acme_tenant() -> uuid.UUID:
    return ACME_TENANT_ID
