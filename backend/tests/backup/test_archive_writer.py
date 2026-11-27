"""Tests for the export archive writer (design doc §3, §9)."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.backup.archive import export_archive
from oc8.backup.secrets_envelope import decrypt_secrets
from oc8.secrets.service import store_secret
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    """`store_secret` (used only by the passphrase-export test below) needs
    `OC8_SECRET_KEK`, matching `tests/secrets/test_service.py`."""
    import base64

    from oc8 import config

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )


def _extract(tar: tarfile.TarFile, member: str | tarfile.TarInfo) -> bytes:
    """`TarFile.extractfile` is typed `IO[bytes] | None`; every member this
    suite asks for was just written into the same archive, so `None` here
    would mean the writer silently dropped a member -- fail loudly."""
    fh = tar.extractfile(member)
    assert fh is not None, f"missing archive member: {member}"
    return fh.read()


async def _seed_tenant(app_session: AppSessionFactory, tenant_id: uuid.UUID, *, slug: str) -> None:
    """A real Organization plus one department/agent pair, named identically
    across tenants in the tests below -- an empty or uniquely-named second
    tenant would prove nothing about isolation."""
    async with app_session(tenant_id) as db:
        db.add(m.Organization(id=tenant_id, slug=slug, name=f"{slug} GmbH", region="eu"))
        await db.flush()
        dept = m.Department(tenant_id=tenant_id, name="Vertrieb", frame={})
        db.add(dept)
        await db.flush()
        db.add(
            m.Agent(
                tenant_id=tenant_id,
                department_id=dept.id,
                name="Nora",
                narrowing={},
                definition={},
                presentation={},
            )
        )


@pytest.fixture
async def acme_tenant(app_session: AppSessionFactory) -> uuid.UUID:
    """A fresh tenant per test, NOT the shared `ACME_TENANT_ID` constant: that
    constant is reused, uncommitted-and-never-rolled-back, across the whole
    suite, so a fresh id is the only way this test's row counts stay exact."""
    tenant_id = uuid.uuid4()
    await _seed_tenant(app_session, tenant_id, slug=f"acme-{tenant_id.hex[:8]}")
    return tenant_id


@pytest.fixture
async def other_tenant(app_session: AppSessionFactory) -> uuid.UUID:
    """A second, independent tenant carrying a department and an agent with
    the SAME names as `acme_tenant`'s -- the collision the isolation test
    needs to be meaningful."""
    tenant_id = uuid.uuid4()
    await _seed_tenant(app_session, tenant_id, slug=f"globex-{tenant_id.hex[:8]}")
    return tenant_id


@pytest.fixture
async def db_session(
    app_session: AppSessionFactory, acme_tenant: uuid.UUID
) -> AsyncIterator[AsyncSession]:
    """An RLS-bound session for `acme_tenant`, matching the tenant being
    exported -- the export reads under the tenant's own `app.tenant_id` GUC
    rather than bypassing RLS with an ad-hoc WHERE clause."""
    async with app_session(acme_tenant) as session:
        yield session


async def test_export_contains_manifest_and_per_table_checksums(
    db_session: AsyncSession, acme_tenant: uuid.UUID
) -> None:
    filename, data = await export_archive(db_session, tenant_id=acme_tenant, passphrase=None)
    assert filename.startswith("oc8-backup-") and filename.endswith(".tar.gz")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        manifest = json.loads(_extract(tar, "manifest.json"))
        assert manifest["format_version"] == 1
        assert manifest["source"]["tenant_id"] == str(acme_tenant)
        assert "region" in manifest["source"]
        assert manifest["secrets"] is None
        assert "audit_event" in manifest["excluded"] and "blobs" in manifest["excluded"]
        assert "organization" not in manifest["tables"]
        assert manifest["alembic_revision"]
        for name, meta in manifest["tables"].items():
            blob = _extract(tar, f"tables/{name}.ndjson")
            assert hashlib.sha256(blob).hexdigest() == meta["sha256"]
            assert blob.count(b"\n") == meta["rows"] or (meta["rows"] == 0 and blob == b"")
        assert manifest["tables"]["agent"]["rows"] == 1
        assert manifest["tables"]["department"]["rows"] == 1


async def test_export_is_deterministic_across_calls(
    db_session: AsyncSession, acme_tenant: uuid.UUID
) -> None:
    """Two exports of an unchanged tenant must be byte-comparable per table
    (stable table order, stable row order) so an operator can diff archives."""
    _, first = await export_archive(db_session, tenant_id=acme_tenant, passphrase=None)
    _, second = await export_archive(db_session, tenant_id=acme_tenant, passphrase=None)
    with tarfile.open(fileobj=io.BytesIO(first), mode="r:gz") as tar1:
        names1 = [ti.name for ti in tar1.getmembers() if ti.name.startswith("tables/")]
        blobs1 = {n: _extract(tar1, n) for n in names1}
    with tarfile.open(fileobj=io.BytesIO(second), mode="r:gz") as tar2:
        names2 = [ti.name for ti in tar2.getmembers() if ti.name.startswith("tables/")]
        blobs2 = {n: _extract(tar2, n) for n in names2}
    assert names1 == names2
    assert blobs1 == blobs2


async def test_export_contains_exactly_one_tenant(
    db_session: AsyncSession, acme_tenant: uuid.UUID, other_tenant: uuid.UUID
) -> None:
    """The load-bearing isolation test (design doc §9): a leak here is a
    cross-customer breach. `other_tenant` owns a department and an agent
    named identically to `acme_tenant`'s before this test means anything.
    Every table file is walked -- not a spot check of two or three -- and
    `acme_tenant`'s own per-table row counts must match its manifest exactly.
    """
    filename, data = await export_archive(db_session, tenant_id=acme_tenant, passphrase=None)
    del filename
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        manifest = json.loads(_extract(tar, "manifest.json"))
        table_members = [ti for ti in tar.getmembers() if ti.name.startswith("tables/")]
        assert table_members, "expected at least one tables/*.ndjson member"
        for member in table_members:
            table_name = member.name.removeprefix("tables/").removesuffix(".ndjson")
            lines = _extract(tar, member).splitlines()
            row_count = 0
            for line in lines:
                if not line:
                    continue
                row = json.loads(line)
                assert row["tenant_id"] == str(acme_tenant), (
                    f"row from another tenant leaked into tables/{table_name}.ndjson: "
                    f"{row['tenant_id']!r}"
                )
                row_count += 1
            assert row_count == manifest["tables"][table_name]["rows"]


async def test_export_with_a_passphrase_writes_a_decryptable_secrets_json(
    db_session: AsyncSession, acme_tenant: uuid.UUID
) -> None:
    """The real Task 5 wiring, not the retired `_stub_encrypt_secrets`:
    `manifest.secrets` is non-null, `secrets.json` is present, and it
    decrypts (via the real `decrypt_secrets`) to exactly the tenant's
    secret values."""
    await store_secret(db_session, tenant_id=acme_tenant, name="openai/api_key", value="sk-abc123")
    await store_secret(db_session, tenant_id=acme_tenant, name="odoo/password", value="hunter2")

    _, data = await export_archive(db_session, tenant_id=acme_tenant, passphrase="op-passphrase")

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        manifest = json.loads(_extract(tar, "manifest.json"))
        assert manifest["secrets"] is not None
        assert manifest["secrets"]["count"] == 2
        assert manifest["secrets"]["kdf"] == "scrypt"
        secrets_bytes = _extract(tar, "secrets.json")

    decrypted = decrypt_secrets(secrets_bytes, "op-passphrase", manifest["secrets"])
    assert decrypted == {"openai/api_key": "sk-abc123", "odoo/password": "hunter2"}


async def test_excluded_list_names_secret_tables_only_when_the_archive_carries_none(
    db_session: AsyncSession, acme_tenant: uuid.UUID
) -> None:
    """An archive built with a passphrase plainly carries `secrets.json` --
    calling `secret`/`tenant_dek` "not part of any backup" beside that file
    would contradict the archive's own contents (final review finding). The
    two must still appear when no passphrase was given at all."""
    await store_secret(db_session, tenant_id=acme_tenant, name="odoo/password", value="hunter2")

    _, without_passphrase = await export_archive(db_session, tenant_id=acme_tenant, passphrase=None)
    with tarfile.open(fileobj=io.BytesIO(without_passphrase), mode="r:gz") as tar:
        manifest = json.loads(_extract(tar, "manifest.json"))
    assert "secret" in manifest["excluded"] and "tenant_dek" in manifest["excluded"]
    assert "audit_event" in manifest["excluded"] and "blobs" in manifest["excluded"]

    _, with_passphrase = await export_archive(
        db_session, tenant_id=acme_tenant, passphrase="op-passphrase"
    )
    with tarfile.open(fileobj=io.BytesIO(with_passphrase), mode="r:gz") as tar:
        manifest = json.loads(_extract(tar, "manifest.json"))
    assert "secret" not in manifest["excluded"] and "tenant_dek" not in manifest["excluded"]
    # What is NOT carried by any channel must still be named.
    assert "audit_event" in manifest["excluded"] and "blobs" in manifest["excluded"]


async def _chat_with_everything(
    db: AsyncSession, tenant_id: uuid.UUID, *, temporary: bool, marker: str
) -> None:
    """A chat with a message, a checkpoint, a file, a knowledge base with a
    chunk and an ingestion job, a queued message to its run, and that run
    itself. `marker` is written into every free-text field, so a test can
    look for it anywhere in the archive."""
    from oc8.chat.files_kb import chat_kb_for
    from oc8.chat.service import create_session

    agent_id = (await db.execute(select(m.Agent.id))).scalars().first()
    assert agent_id is not None
    session = await create_session(
        db, tenant_id=tenant_id, agent_id=agent_id, member_id=uuid.uuid4(), temporary=temporary
    )
    message = m.ChatMessage(tenant_id=tenant_id, session_id=session.id, role="user", content=marker)
    db.add(message)
    await db.flush()
    db.add(
        m.ChatCheckpoint(
            tenant_id=tenant_id, session_id=session.id, upto_message_id=message.id,
            summary=marker, token_estimate=1, model="m",
        )
    )  # fmt: skip
    db.add(
        m.FileAttachment(
            tenant_id=tenant_id, owner_type="chat_message", owner_id=message.id,
            bucket_key=f"chat/{uuid.uuid4()}", filename=f"{marker}.txt",
            content_type="text/plain", size_bytes=1, extracted_text=marker,
        )
    )  # fmt: skip
    kb = await chat_kb_for(db, session=session, create=True)
    assert kb is not None
    source_id = uuid.UUID(str(kb.source_ids[0]))
    db.add(
        m.KbChunk(
            tenant_id=tenant_id, kb_id=kb.id, data_source_id=source_id, content=marker,
            source_uri=f"chat://{marker}",
        )
    )  # fmt: skip
    db.add(m.IngestionJob(tenant_id=tenant_id, data_source_id=source_id, kb_id=kb.id))
    run = m.AgentRun(
        tenant_id=tenant_id, agent_id=agent_id, source="chat", state="done",
        context={"chat_session_id": str(session.id)},
    )  # fmt: skip
    db.add(run)
    await db.flush()
    db.add(
        m.RunInput(tenant_id=tenant_id, run_id=run.id, author_member_id=uuid.uuid4(), text=marker)
    )
    await db.flush()


async def test_a_temporary_chat_is_not_in_the_backup_but_its_runs_are(
    db_session: AsyncSession, acme_tenant: uuid.UUID
) -> None:
    """A temporary chat is a conversation that is not kept (spec §5.6): a
    backup taken while one is open must not carry its messages, checkpoints,
    files, knowledge or queued messages -- the archive would keep them long
    after the chat is gone. Its runs are governance and stay, as they do when
    the chat is deleted. An ordinary chat beside it is backed up whole."""
    await _chat_with_everything(db_session, acme_tenant, temporary=True, marker="ephemeral-xyz")
    await _chat_with_everything(db_session, acme_tenant, temporary=False, marker="kept-abc")

    _, data = await export_archive(db_session, tenant_id=acme_tenant, passphrase=None)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        manifest = json.loads(_extract(tar, "manifest.json"))
        blobs = {
            ti.name.removeprefix("tables/").removesuffix(".ndjson"): _extract(tar, ti)
            for ti in tar.getmembers()
            if ti.name.startswith("tables/")
        }

    leaked = sorted(name for name, blob in blobs.items() if b"ephemeral-xyz" in blob)
    assert leaked == [], f"the temporary chat's content is in the backup: {leaked}"
    for name in ("chat_session", "chat_message", "chat_checkpoint", "file_attachment"):
        assert manifest["tables"][name]["rows"] == 1, name
    for name in ("knowledge_base", "data_source", "kb_chunk", "ingestion_job", "run_input"):
        assert manifest["tables"][name]["rows"] == 1, name
    assert all(b"kept-abc" in blobs[name] for name in ("chat_message", "kb_chunk", "run_input"))
    assert manifest["tables"]["agent_run"]["rows"] == 2, "both chats' runs stay (governance)"
