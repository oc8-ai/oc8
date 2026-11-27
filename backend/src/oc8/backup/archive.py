"""Building the export archive (design doc §3). Read-only: selects rows,
never writes, so it is safe to call inside a request that does other things
in the same transaction.

Never commits: the caller (the API endpoint) owns the transaction boundary,
exactly like `oc8.secrets.service` -- the Postgres RLS GUC (`app.tenant_id`)
is transaction-local, and a mid-service commit would unbind it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import tarfile
import uuid
from typing import Any

from sqlalchemy import ColumnElement, String, and_, cast, not_, or_, select, text, union
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.backup.errors import BackupError
from oc8.backup.secrets_envelope import encrypt_secrets
from oc8.backup.serialization import serialize_row
from oc8.backup.tables import EXCLUDED_TABLES, dependency_order, exported_tables, table_by_name

FORMAT_VERSION = 1


def excluded_for_manifest(*, secrets_included: bool) -> list[str]:
    """manifest.json's "excluded" field (design doc §3's example): the denylist
    (Task 1's `EXCLUDED_TABLES`, so a rename there cannot silently drift this
    list) plus "blobs" -- the design doc §2.3 catch-all for what never becomes
    a row at all (run evidence tarballs, knowledge raw objects: filesystem/
    object-store content, not a table name).

    `secret`/`tenant_dek` are dropped from the list when `secrets_included` is
    true. Those two tables are always excluded from the table dump -- secrets
    travel through the separate `secrets.json` envelope instead (§4) -- but
    when that envelope IS present, the credentials plainly did travel with
    the archive, so calling them "not part of any backup" would contradict
    the archive's own contents. `secrets_included` mirrors
    `RestorePreview.has_secrets`: ciphertext presence, not secret count, so a
    zero-secret tenant exported with a passphrase stays consistent between
    the two.
    """
    tables = EXCLUDED_TABLES - {"secret", "tenant_dek"} if secrets_included else EXCLUDED_TABLES
    return [*sorted(tables), "blobs"]


def _not_of_a_temporary_chat(name: str, tenant_id: uuid.UUID) -> list[ColumnElement[bool]]:
    """The extra WHERE that keeps a temporary chat out of `name`'s rows (spec
    §5.6: a conversation that is not kept). An archive outlives the chat, so
    its session, messages, checkpoints, files, knowledge base (source, chunks,
    jobs, grants) and the messages queued to its runs stay out -- the same
    rows `chat.service.delete_session` removes or empties. Its runs,
    approvals, audit events and usage are governance and are exported as
    always. Nothing outside this set references these rows by a real foreign
    key, so the restore's insert order is unaffected."""
    session = table_by_name("chat_session")
    temporary = select(session.c.id).where(
        session.c.tenant_id == tenant_id, session.c.temporary.is_(True)
    )
    temporary_text = select(cast(session.c.id, String)).where(
        session.c.tenant_id == tenant_id, session.c.temporary.is_(True)
    )
    if name == "chat_session":
        return [session.c.temporary.is_(False)]
    table = table_by_name(name)
    if name in ("chat_message", "chat_checkpoint"):
        return [table.c.session_id.notin_(temporary)]
    if name == "direct_access_grant":
        return [table.c.chat_session_id.notin_(temporary)]
    if name == "knowledge_base":
        return [or_(table.c.chat_session_id.is_(None), table.c.chat_session_id.notin_(temporary))]
    kb = table_by_name("knowledge_base")
    temporary_kbs = select(kb.c.id).where(kb.c.chat_session_id.in_(temporary))
    source = table_by_name("data_source")
    temporary_sources = select(source.c.id).where(
        source.c.tenant_id == tenant_id,
        source.c.config["chat_session_id"].astext.in_(temporary_text),
    )
    if name in ("kb_chunk", "knowledge_grant"):
        return [table.c.kb_id.notin_(temporary_kbs)]
    if name == "data_source":
        return [table.c.id.notin_(temporary_sources)]
    if name == "ingestion_job":
        return [table.c.data_source_id.notin_(temporary_sources)]
    if name == "file_attachment":
        message = table_by_name("chat_message")
        owners = union(temporary, select(message.c.id).where(message.c.session_id.in_(temporary)))
        return [not_(and_(table.c.owner_type == "chat_message", table.c.owner_id.in_(owners)))]
    if name == "run_input":
        run = table_by_name("agent_run")
        temporary_runs = select(run.c.id).where(
            run.c.tenant_id == tenant_id,
            run.c.context["chat_session_id"].astext.in_(temporary_text),
        )
        return [table.c.run_id.notin_(temporary_runs)]
    return []


async def current_alembic_revision(db: AsyncSession) -> str:
    """The live head, read from the database rather than hardcoded, so a
    forgotten bump here can never quietly diverge from what actually
    migrated (design doc §3.1: import refuses unless this matches)."""
    row = (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
    return str(row)


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


async def export_archive(
    db: AsyncSession, *, tenant_id: uuid.UUID, passphrase: str | None
) -> tuple[str, bytes]:
    """Build the whole archive in memory and return `(filename, gzip_bytes)`.

    Whole-archive-in-memory rather than a chunked stream, per design doc §6:
    async job machinery is premature until a real archive is shown to be
    slow, and a pilot-sized company's archive fits in memory.

    Reads happen on `db`, the caller's own RLS-bound session -- rows are
    scoped to `tenant_id` by both the explicit WHERE clause below AND the
    database's own `tenant_isolation` policy (`app.tenant_id` must already
    equal `tenant_id` in this session), so isolation is enforced twice
    rather than only by application code.
    """
    organization = await db.get(m.Organization, tenant_id)
    if organization is None:
        raise BackupError(f"no organization found for tenant {tenant_id}")

    tables = exported_tables()
    order = await dependency_order(db, tables)

    table_bytes: dict[str, bytes] = {}
    table_meta: dict[str, dict[str, object]] = {}
    for name in order:
        table = table_by_name(name)
        result = await db.execute(
            select(table)
            .where(table.c.tenant_id == tenant_id, *_not_of_a_temporary_chat(name, tenant_id))
            .order_by(table.c.id)
        )
        rows = result.mappings().all()
        lines = [json.dumps(serialize_row(table, dict(row)), sort_keys=True) for row in rows]
        blob = ("\n".join(lines) + ("\n" if lines else "")).encode()
        table_bytes[name] = blob
        table_meta[name] = {"rows": len(rows), "sha256": hashlib.sha256(blob).hexdigest()}

    manifest: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "alembic_revision": await current_alembic_revision(db),
        "source": {
            "tenant_id": str(tenant_id),
            "slug": organization.slug,
            "name": organization.name,
            "region": organization.region,
        },
        "tables": table_meta,
        "secrets": None,
        # `bool(passphrase)` matches the `if passphrase:` gate below verbatim
        # (and `RestorePreview.has_secrets`'s own definition) -- whether
        # `secrets.json` ends up in this archive at all, not how many secrets
        # it holds.
        "excluded": excluded_for_manifest(secrets_included=bool(passphrase)),
    }

    secrets_bytes: bytes | None = None
    if passphrase:
        from oc8.secrets.service import resolve_secret

        secret_names = (
            (await db.execute(select(m.Secret.name).where(m.Secret.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
        plaintext = {
            name: await resolve_secret(db, tenant_id=tenant_id, ref=name) for name in secret_names
        }
        secrets_bytes, secrets_meta = encrypt_secrets(plaintext, passphrase)
        manifest["secrets"] = secrets_meta

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _add_bytes(tar, "manifest.json", json.dumps(manifest, indent=2, sort_keys=True).encode())
        for name in order:
            _add_bytes(tar, f"tables/{name}.ndjson", table_bytes[name])
        if secrets_bytes is not None:
            _add_bytes(tar, "secrets.json", secrets_bytes)

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
    filename = f"oc8-backup-{organization.slug}-{stamp}.tar.gz"
    return filename, buf.getvalue()
