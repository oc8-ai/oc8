"""Knowledge bases, data sources, and document ingestion (§11, minimal loop)."""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.api.v1._listquery import apply_group_order, apply_search, paginate
from oc8.api.v1._serializers import (
    chunk_to_dto,
    datasource_to_dto,
    ingestion_job_to_dto,
    kb_to_dto,
    knowledge_document_to_dto,
    similar_chunk_to_dto,
)
from oc8.authz.permissions import KNOWLEDGE, MANAGE, VIEW, perm
from oc8.caching.department_cache import purge_tenant
from oc8.knowledge.chunks import linked_source_ids, list_chunks, list_documents
from oc8.knowledge.connectors.base import ConnectorError
from oc8.knowledge.connectors.context import SourceAuthContext
from oc8.knowledge.connectors.fetcher import safe_fetch
from oc8.knowledge.connectors.registry import available_connectors, resolve_connector
from oc8.knowledge.external_index import ExternalIndexRejected, validate_external_index_binding
from oc8.knowledge.ingest import (
    IngestionError,
    KnowledgeBaseNotFoundError,
    extract_text,
    ingest_document,
)
from oc8.knowledge.retrieval import similar_chunks
from oc8.knowledge.sources import SourceRejected, validate_source_config
from oc8.knowledge.tombstone import (
    OPERATOR_DELETE,
    restore_document,
    tombstone_base,
    tombstone_document,
    tombstone_source,
    unlink_source_from_base,
)
from oc8.knowledge.vector_indexes.base import VectorIndexError
from oc8.knowledge.vector_indexes.registry import (
    INTERNAL_INDEX_TYPE,
    available_vector_indexes,
    resolve_vector_index,
)
from oc8.knowledge.worker import get_ingestion_queue
from oc8.modelrouter import EmbeddingUnavailable, get_model_router
from oc8.schemas.base import CamelModel
from oc8.schemas.dto import (
    BaseRemovalDTO,
    DataSourceDTO,
    DocumentRemovalDTO,
    DocumentRestoreDTO,
    GrantDTO,
    IngestionJobDTO,
    KbChunkDTO,
    KnowledgeBaseDTO,
    KnowledgeDocumentDTO,
    SimilarChunkDTO,
    SourceRemovalDTO,
    SourceUnlinkedFromBaseDTO,
)
from oc8.schemas.paging import Page
from oc8.schemas.requests import (
    CreateGrantRequest,
    CreateKnowledgeBaseRequest,
    CreateSourceRequest,
    IngestDocumentRequest,
    SyncSourceRequest,
    UpdateKnowledgeBaseRequest,
    UpdateSourceRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Writes are org_admin-only: a data source can carry an OAuth connection to a
# customer's cloud account, so an agent- or plugin-kind principal must not be
# able to create one or trigger a sync. Reads stay open -- agents retrieve.


class ConnectorCatalogDTO(CamelModel):
    """A tenant's usable source connectors and their declarative form schema."""

    type_id: str
    label: str | None = None
    description: str | None = None
    config_schema: dict[str, object]
    requires_oauth: str | None = None


class VectorIndexCatalogDTO(CamelModel):
    """A tenant's usable query-only vector-index backends."""

    type_id: str
    label: str | None = None
    description: str | None = None
    config_schema: dict[str, object]
    credential_type: str


class PreviewIndexRequest(CamelModel):
    query: str = "test"
    limit: int = 5


@router.get(
    "/knowledge/connectors",
    response_model=list[ConnectorCatalogDTO],
    dependencies=[Depends(require_permission(perm(KNOWLEDGE, VIEW)))],
)
async def list_connectors(db: DbSession, principal: CurrentPrincipal) -> list[ConnectorCatalogDTO]:
    connectors = await available_connectors(db, tenant_id=principal.tenant_id)
    return [
        ConnectorCatalogDTO(
            type_id=type_id,
            label=getattr(connector, "label", None),
            description=getattr(connector, "description", None),
            config_schema=connector.config_schema,
            requires_oauth=connector.requires_oauth,
        )
        for type_id, connector in sorted(connectors.items())
        # Uploads are created by document upload, not as a configurable source.
        if type_id != "upload"
    ]


@router.get(
    "/knowledge/vector-indexes",
    response_model=list[VectorIndexCatalogDTO],
    dependencies=[Depends(require_permission(perm(KNOWLEDGE, VIEW)))],
)
async def list_vector_indexes(
    db: DbSession, principal: CurrentPrincipal
) -> list[VectorIndexCatalogDTO]:
    indexes = await available_vector_indexes(db, tenant_id=principal.tenant_id)
    return [
        VectorIndexCatalogDTO(
            type_id=type_id,
            label=getattr(index, "label", None),
            description=getattr(index, "description", None),
            config_schema=index.config_schema,
            credential_type=index.credential_type,
        )
        for type_id, index in sorted(indexes.items())
    ]


async def _live_base(db: DbSession, kb_id: uuid.UUID) -> m.KnowledgeBase:
    """The base, or a 404. A base another tenant owns is invisible under RLS and
    a deleted one is treated the same way: "no such base" and "you may not touch
    that base" must read identically, or the 404 confirms it exists."""
    kb = await db.get(m.KnowledgeBase, kb_id)
    if kb is None or kb.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "knowledge base not found")
    return kb


async def _live_source(db: DbSession, source_id: uuid.UUID, tenant_id: uuid.UUID) -> m.DataSource:
    ds = await db.get(m.DataSource, source_id)
    if ds is None or ds.tenant_id != tenant_id or ds.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found")
    return ds


async def _grants_for(db: DbSession) -> dict[uuid.UUID, tuple[list[str], list[str]]]:
    grants = (await db.execute(select(m.KnowledgeGrant))).scalars().all()
    out: dict[uuid.UUID, tuple[list[str], list[str]]] = {}
    for g in grants:
        depts, agents = out.setdefault(g.kb_id, ([], []))
        if g.grantee_type == "department":
            depts.append(str(g.grantee_id))
        else:
            agents.append(str(g.grantee_id))
    return out


async def _linked_sources_for(db: DbSession) -> dict[uuid.UUID, list[str]]:
    """Thin string-ifying wrapper: `api/v1/knowledge.py` is not on the
    `KbChunk` read-path allowlist (`tests/knowledge/test_read_path_guard.py`),
    so the actual query lives in `knowledge/chunks.py`."""
    return {
        kb_id: [str(source_id) for source_id in source_ids]
        for kb_id, source_ids in (await linked_source_ids(db)).items()
    }


@router.get(
    "/knowledge/bases",
    response_model=Page[KnowledgeBaseDTO],
    dependencies=[Depends(require_permission(perm(KNOWLEDGE, VIEW)))],
)
async def list_bases(
    db: DbSession,
    search: str | None = None,
    group_by: str | None = None,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    # A deleted base is retired, not vaporised -- its tombstoned chunks still
    # point at it -- but it must not keep appearing in the product as
    # something an operator can still fill, unless asked for explicitly. Same
    # flag, same default and the same name as list_sources'.
    include_archived: bool = Query(False, alias="includeArchived"),
) -> Page[KnowledgeBaseDTO]:
    grants = await _grants_for(db)
    sources = await _linked_sources_for(db)
    stmt = select(m.KnowledgeBase)
    if not include_archived:
        stmt = stmt.where(m.KnowledgeBase.deleted_at.is_(None))
    stmt = apply_search(
        stmt,
        model=m.KnowledgeBase,
        columns=[m.KnowledgeBase.name, m.KnowledgeBase.description],
        search=search,
    )
    stmt = apply_group_order(
        stmt,
        model=m.KnowledgeBase,
        group_by=group_by,
        group_fields={"sensitivity": m.KnowledgeBase.classification},
        default_order=m.KnowledgeBase.created_at,
    )
    rows, total = await paginate(db, stmt, limit=limit, offset=offset)
    return Page(
        items=[
            kb_to_dto(kb, *grants.get(kb.id, ([], [])), sources.get(kb.id, [])) for kb in rows
        ],
        total_count=total,
    )


@router.get(
    "/knowledge/bases/{kb_id}",
    response_model=KnowledgeBaseDTO,
    dependencies=[Depends(require_permission(perm(KNOWLEDGE, VIEW)))],
)
async def get_base(kb_id: uuid.UUID, db: DbSession) -> KnowledgeBaseDTO:
    kb = await _live_base(db, kb_id)
    grants = await _grants_for(db)
    sources = await _linked_sources_for(db)
    return kb_to_dto(kb, *grants.get(kb.id, ([], [])), sources.get(kb.id, []))


@router.post(
    "/knowledge/bases",
    response_model=KnowledgeBaseDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(KNOWLEDGE, MANAGE)))],
)
async def create_base(
    body: CreateKnowledgeBaseRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> KnowledgeBaseDTO:
    index_type = (body.index_type or INTERNAL_INDEX_TYPE).strip() or INTERNAL_INDEX_TYPE
    index_config = dict(body.index_config or {})
    credential_id = body.credential_id

    if index_type == INTERNAL_INDEX_TYPE:
        credential_id = None
        index_config = {}
    else:
        try:
            await validate_external_index_binding(
                db,
                tenant_id=principal.tenant_id,
                index_type=index_type,
                index_config=index_config,
                credential_id=credential_id,
            )
        except ExternalIndexRejected as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    kb = m.KnowledgeBase(
        tenant_id=principal.tenant_id,
        name=body.name,
        description=body.description,
        embedding_model=body.embedding_model,
        status="current",
        classification=body.classification or "internal",
        index_type=index_type,
        index_config=index_config,
        credential_id=credential_id,
    )
    db.add(kb)
    await db.flush()
    return kb_to_dto(kb, [], [])


@router.post(
    "/knowledge/bases/{kb_id}/preview-index",
    dependencies=[Depends(require_permission(perm(KNOWLEDGE, MANAGE)))],
)
async def preview_index(
    kb_id: uuid.UUID,
    body: PreviewIndexRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> dict[str, object]:
    """Probe-search an external index. Internal bases use the documents list."""
    kb = await _live_base(db, kb_id)
    if (kb.index_type or INTERNAL_INDEX_TYPE) == INTERNAL_INDEX_TYPE:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "preview-index is only for external vector indexes",
        )
    if kb.credential_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "knowledge base has no credential")
    try:
        index = await resolve_vector_index(
            db, tenant_id=principal.tenant_id, type_id=kb.index_type
        )
        query_embedding = await get_model_router().embed(body.query, model=kb.embedding_model)
        hits = await index.search(
            kb.index_config or {},
            SourceAuthContext(db, tenant_id=principal.tenant_id),
            credential_id=str(kb.credential_id),
            query_embedding=query_embedding,
            query_text=body.query,
            limit=max(1, min(body.limit, 20)),
        )
    except EmbeddingUnavailable as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"embedding unavailable for model {kb.embedding_model!r}",
        ) from exc
    except VectorIndexError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {
        "hits": [
            {
                "content": h.content,
                "sourceUri": h.source_uri,
                "score": h.score,
                "classification": h.classification,
            }
            for h in hits
        ]
    }


@router.patch(
    "/knowledge/bases/{kb_id}",
    response_model=KnowledgeBaseDTO,
    dependencies=[Depends(require_permission(perm(KNOWLEDGE, MANAGE)))],
)
async def update_base(
    kb_id: uuid.UUID, body: UpdateKnowledgeBaseRequest, db: DbSession
) -> KnowledgeBaseDTO:
    """Edit a base's name, description, or (only before it has any content)
    its embedding model. Not the DELETE below: that one is irreversible by
    design (§12.5.1). This is an ordinary metadata edit."""
    kb = await _live_base(db, kb_id)
    if body.name is not None:
        kb.name = body.name
    if body.description is not None:
        kb.description = body.description
    if body.embedding_model is not None and body.embedding_model != kb.embedding_model:
        chunks = (kb.freshness or {}).get("chunks", 0)
        if chunks:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "this base already has ingested content under its current embedding "
                "model -- changing models now would leave old and new chunks "
                "incomparable in the same base. Delete and recreate it instead.",
            )
        kb.embedding_model = body.embedding_model
    await db.flush()
    # Build the DTO from data already fetched, not a post-commit re-fetch: a
    # commit here unbinds the tenant GUC that RLS relies on (see
    # update_source), so any SELECT issued after it would 500 or return
    # nothing. `_grants_for`/`_linked_sources_for` must run before the commit
    # for the same reason.
    grants = await _grants_for(db)
    sources = await _linked_sources_for(db)
    dto = kb_to_dto(kb, *grants.get(kb.id, ([], [])), sources.get(kb.id, []))
    await db.commit()
    return dto


@router.post(
    "/knowledge/bases/{kb_id}/documents",
    response_model=KnowledgeBaseDTO,
    dependencies=[Depends(require_permission(perm(KNOWLEDGE, MANAGE)))],
)
async def upload_document(
    kb_id: uuid.UUID,
    body: IngestDocumentRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> KnowledgeBaseDTO:
    try:
        await ingest_document(
            db,
            tenant_id=principal.tenant_id,
            kb_id=kb_id,
            filename=body.filename,
            content=body.content,
            content_type=body.content_type,
        )
    except KnowledgeBaseNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except IngestionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    kb = await db.get(m.KnowledgeBase, kb_id)
    assert kb is not None
    grants = await _grants_for(db)
    sources = await _linked_sources_for(db)
    return kb_to_dto(kb, *grants.get(kb.id, ([], [])), sources.get(kb.id, []))


@router.get(
    "/knowledge/sources",
    response_model=Page[DataSourceDTO],
    dependencies=[Depends(require_permission(perm(KNOWLEDGE, VIEW)))],
)
async def list_sources(
    db: DbSession,
    search: str | None = None,
    connector_type: str | None = Query(None, alias="connectorType"),
    group_by: str | None = None,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    # Retired sources keep their IngestionJob rows and their tombstones; they
    # do not keep their place in the default source list. Same flag, same
    # default and the same name as list_skills' -- one contract, everywhere a
    # SoftDeleteMixin model is listed.
    include_archived: bool = Query(False, alias="includeArchived"),
) -> Page[DataSourceDTO]:
    stmt = select(m.DataSource)
    if not include_archived:
        stmt = stmt.where(m.DataSource.deleted_at.is_(None))
    if connector_type:
        stmt = stmt.where(m.DataSource.connector_type == connector_type)
    stmt = apply_search(stmt, model=m.DataSource, columns=[m.DataSource.name], search=search)
    stmt = apply_group_order(
        stmt,
        model=m.DataSource,
        group_by=group_by,
        group_fields={"connectorType": m.DataSource.connector_type},
        default_order=m.DataSource.created_at,
    )
    rows, total = await paginate(db, stmt, limit=limit, offset=offset)
    return Page(items=[datasource_to_dto(d) for d in rows], total_count=total)


@router.post(
    "/knowledge/sources",
    response_model=DataSourceDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(KNOWLEDGE, MANAGE)))],
)
async def create_source(
    body: CreateSourceRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> DataSourceDTO:
    # Shared with the plugin setup form's own source creation
    # (api/v1/capas.py), which was building the row by hand and running
    # neither check. See oc8/knowledge/sources.py.
    try:
        await validate_source_config(
            db,
            tenant_id=principal.tenant_id,
            connector_type=body.connector_type,
            config=body.config,
            oauth_connection_id=body.oauth_connection_id,
        )
    except ConnectorError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except SourceRejected as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if body.oauth_connection_id is not None:
        conn = await db.get(m.OAuthConnection, body.oauth_connection_id)
        if conn is None or conn.tenant_id != principal.tenant_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "oauth connection not found")
    ds = m.DataSource(
        tenant_id=principal.tenant_id,
        connector_type=body.connector_type,
        name=body.name,
        config=body.config,
        classification=body.classification,
        oauth_connection_id=body.oauth_connection_id,
        connected=True,
    )
    db.add(ds)
    await db.flush()
    await db.commit()
    return datasource_to_dto(ds)


@router.patch(
    "/knowledge/sources/{source_id}",
    response_model=DataSourceDTO,
    dependencies=[Depends(require_permission(perm(KNOWLEDGE, MANAGE)))],
)
async def update_source(
    source_id: uuid.UUID,
    body: UpdateSourceRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> DataSourceDTO:
    """Edit a source's name, classification, or schedule. Not the DELETE below:
    that one is irreversible by design (§12.5.1 -- an operator DELETE destroys
    the source's content immediately). This is an ordinary metadata edit, on a
    row that stays exactly as reachable afterward as before."""
    ds = await _live_source(db, source_id, principal.tenant_id)
    if body.name is not None:
        ds.name = body.name
    if body.classification is not None:
        ds.classification = body.classification
    if body.schedule_cron is not None:
        ds.schedule_cron = body.schedule_cron
    if body.config is not None:
        ds.config = {**(ds.config or {}), **body.config}
    await db.flush()
    # Build the DTO from the in-memory object, not a post-commit re-fetch: a
    # commit here unbinds the tenant GUC that RLS relies on (see create_source,
    # sync_source), so any SELECT issued after it would 500 or return nothing.
    dto = datasource_to_dto(ds)
    await db.commit()
    return dto


@router.post(
    "/knowledge/sources/{source_id}/preview",
    dependencies=[Depends(require_permission(perm(KNOWLEDGE, MANAGE)))],
)
async def preview_source(
    source_id: uuid.UUID,
    db: DbSession,
    principal: CurrentPrincipal,
) -> dict[str, object]:
    ds = await db.get(m.DataSource, source_id)
    if ds is None or ds.tenant_id != principal.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found")
    try:
        connector = await resolve_connector(
            db, tenant_id=principal.tenant_id, type_id=ds.connector_type
        )
        items = await connector.discover(
            ds.config,
            SourceAuthContext(
                db, tenant_id=principal.tenant_id, connection_id=ds.oauth_connection_id
            ),
        )
        sample_text = ""
        if items:
            uri = items[0].uri
            if urlparse(uri).scheme in ("http", "https"):
                raw_text, ctype = await safe_fetch(uri)
                try:
                    sample_text = extract_text(content=raw_text, content_type=ctype)[:2000]
                except IngestionError:
                    # Unsupported sample content-type (e.g. application/json):
                    # still return the discovered items, just without a preview.
                    sample_text = ""
    except ConnectorError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"items": [{"uri": i.uri, "title": i.title} for i in items], "sampleText": sample_text}


@router.post(
    "/knowledge/sources/{source_id}/sync",
    response_model=IngestionJobDTO,
    dependencies=[Depends(require_permission(perm(KNOWLEDGE, MANAGE)))],
)
async def sync_source(
    source_id: uuid.UUID,
    body: SyncSourceRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> IngestionJobDTO:
    # A deleted source is refused here and again in `worker.ingest_job`: a sync
    # started after the tombstones landed would re-ingest straight back into the
    # base the operator just cleared.
    ds = await _live_source(db, source_id, principal.tenant_id)
    kb = await _live_base(db, body.kb_id)
    if (kb.index_type or INTERNAL_INDEX_TYPE) != INTERNAL_INDEX_TYPE:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "this knowledge base is connected to an external vector index — "
            "sync is not supported",
        )

    job = m.IngestionJob(
        tenant_id=principal.tenant_id,
        data_source_id=ds.id,
        kb_id=body.kb_id,
        status="queued",
    )
    db.add(job)
    await db.flush()
    # Commit before enqueue: the worker's XREADGROUP can fire instantly, so the
    # queued row must be durable first (mirrors runtime.intake.enqueue_run).
    await db.commit()
    await get_ingestion_queue().enqueue(run_id=job.id, tenant_id=principal.tenant_id)
    return ingestion_job_to_dto(job)


# ------------------------------------------------------------------ removal
#
# Until these five routes existed a customer could put a document into a
# knowledge base and never take it out: there was no DELETE anywhere on the
# knowledge API, so the only removal path in the product was raw SQL against
# `kb_chunk`. They are also the whole answer for a GDPR-shaped request --
# nothing schedules a sync, so the reconciliation sweep may never fire for a
# given source.
#
# `sourceUri` is a QUERY parameter, not a path segment: `upload://a/b` has
# slashes and a URI in a path is an encoding minefield. `dataSourceId` is
# optional and NARROWING -- omitted, it reaches every source's copy of that URI
# in that base, including pre-0045 rows whose provenance the migration honestly
# refused to guess, and the response reports the blast radius.


@router.get(
    "/knowledge/bases/{kb_id}/documents",
    response_model=list[KnowledgeDocumentDTO],
    dependencies=[Depends(require_permission(perm(KNOWLEDGE, VIEW)))],
)
async def list_kb_documents(
    kb_id: uuid.UUID,
    db: DbSession,
    principal: CurrentPrincipal,
    include_deleted: bool = Query(False, alias="includeDeleted"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[KnowledgeDocumentDTO]:
    """What this base holds, one row per document.

    It ships with the DELETE below rather than after it: `source_uri` appears in
    no other DTO, response or frontend file, and for an upload it carries a uuid
    the server invented -- so on its own the delete route would be addressable
    by nobody.
    """
    await _live_base(db, kb_id)
    docs = await list_documents(
        db,
        tenant_id=principal.tenant_id,
        kb_id=kb_id,
        include_deleted=include_deleted,
        limit=limit,
        offset=offset,
    )
    return [knowledge_document_to_dto(d) for d in docs]


@router.get(
    "/knowledge/bases/{kb_id}/chunks",
    response_model=Page[KbChunkDTO],
    dependencies=[Depends(require_permission(perm(KNOWLEDGE, MANAGE)))],
)
async def list_kb_chunks(
    kb_id: uuid.UUID,
    db: DbSession,
    principal: CurrentPrincipal,
    search: str | None = None,
    source_uri: str | None = Query(None, alias="sourceUri"),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[KbChunkDTO]:
    """What a base actually knows, chunk by chunk. Gated at MANAGE, not VIEW --
    see this plan's Global Constraints for why no per-chunk classification
    filter exists yet."""
    await _live_base(db, kb_id)
    rows, total = await list_chunks(
        db,
        tenant_id=principal.tenant_id,
        kb_id=kb_id,
        search=search,
        source_uri=source_uri,
        limit=limit,
        offset=offset,
    )
    return Page(items=[chunk_to_dto(r) for r in rows], total_count=total)


@router.get(
    "/knowledge/bases/{kb_id}/chunks/{chunk_id}/similar",
    response_model=list[SimilarChunkDTO],
    dependencies=[Depends(require_permission(perm(KNOWLEDGE, MANAGE)))],
)
async def get_similar_chunks(
    kb_id: uuid.UUID,
    chunk_id: uuid.UUID,
    db: DbSession,
    principal: CurrentPrincipal,
) -> list[SimilarChunkDTO]:
    """The chunks nearest to one chunk by cosine similarity, same KB, live
    only. Gated at MANAGE like the listing route above."""
    await _live_base(db, kb_id)
    rows = await similar_chunks(db, tenant_id=principal.tenant_id, kb_id=kb_id, chunk_id=chunk_id)
    if rows is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "chunk not found")
    return [similar_chunk_to_dto(r) for r in rows]


async def _purge_department_cache(tenant_id: uuid.UUID) -> None:
    """Drop this tenant's department prompt-cache after an OPERATOR-irreversible
    deletion. A cached agent answer generated from content that has just been
    destroyed must not keep being served for the rest of its 24h TTL, and the
    cache has no per-chunk invalidation to do anything finer.

    Best-effort and fail-open on purpose: the deletion is already committed and
    irreversible either way, so a Redis failure must never turn into an error
    response for something that already happened. Kept out of
    `oc8.knowledge.tombstone`, which stays a pure-DB, infra-free layer whose
    functions are flush-only -- the commit, and therefore this, belong to the
    caller at the edge of the request."""
    try:
        await purge_tenant(tenant_id)
    except Exception:
        logger.warning("department cache purge failed after operator deletion", exc_info=True)


@router.delete(
    "/knowledge/bases/{kb_id}/documents",
    response_model=DocumentRemovalDTO,
    dependencies=[Depends(require_permission(perm(KNOWLEDGE, MANAGE)))],
)
async def delete_kb_document(
    kb_id: uuid.UUID,
    db: DbSession,
    principal: CurrentPrincipal,
    source_uri: str = Query(..., alias="sourceUri"),
    data_source_id: uuid.UUID | None = Query(None, alias="dataSourceId"),
) -> DocumentRemovalDTO:
    """Erase one document. A human named the object, so this is irreversible:
    the content is destroyed in the same transaction that tombstones it and the
    digest of what was destroyed goes into the tenant's hash chain."""
    await _live_base(db, kb_id)
    removal = await tombstone_document(
        db,
        tenant_id=principal.tenant_id,
        kb_id=kb_id,
        data_source_id=data_source_id,
        source_uri=source_uri,
        reason=OPERATOR_DELETE,
        reduce_now=True,
        now=dt.datetime.now(tz=dt.UTC),
        principal=principal,
    )
    if removal is None:
        # 404 rather than 204 on a URI that matched nothing: "it's gone" and
        # "you typed it wrong" must not be the same answer, and a success here
        # would have appended a permanent, unrepairable ledger row claiming a
        # deletion that never happened.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    # Committed here, not by the caller: `tombstone_document` is flush-only
    # because the tenant GUC that backs RLS is transaction-local, so the commit
    # belongs at the edge of the request (same as create_source and sync_source).
    await db.commit()
    await _purge_department_cache(principal.tenant_id)
    return DocumentRemovalDTO(
        source_uri=source_uri,
        sources_touched=removal.sources,
        documents=removal.documents,
        chunks=removal.chunks,
        sha256=removal.sha256,
        audit_seq=removal.audit_seq,
    )


@router.post(
    "/knowledge/bases/{kb_id}/documents/restore",
    response_model=DocumentRestoreDTO,
    dependencies=[Depends(require_permission(perm(KNOWLEDGE, MANAGE)))],
)
async def restore_kb_document(
    kb_id: uuid.UUID,
    db: DbSession,
    principal: CurrentPrincipal,
    source_uri: str = Query(..., alias="sourceUri"),
    data_source_id: uuid.UUID | None = Query(None, alias="dataSourceId"),
) -> DocumentRestoreDTO:
    """Undo a deletion the system INFERRED, within its grace window.

    Only un-reduced tombstones are reachable, which by construction means the
    reason was `source_absent` or `superseded`. An operator delete has nothing
    left to bring back, and answers 404 like any other URI that matches nothing.
    """
    await _live_base(db, kb_id)
    chunks = await restore_document(
        db,
        tenant_id=principal.tenant_id,
        kb_id=kb_id,
        data_source_id=data_source_id,
        source_uri=source_uri,
        now=dt.datetime.now(tz=dt.UTC),
        principal=principal,
    )
    if chunks == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no restorable document")
    await db.commit()
    return DocumentRestoreDTO(source_uri=source_uri, chunks=chunks)


@router.delete(
    "/knowledge/sources/{source_id}",
    response_model=SourceRemovalDTO,
    dependencies=[Depends(require_permission(perm(KNOWLEDGE, MANAGE)))],
)
async def delete_source(
    source_id: uuid.UUID,
    db: DbSession,
    principal: CurrentPrincipal,
) -> SourceRemovalDTO:
    """Erase everything one source ever ingested, and retire the source."""
    ds = await _live_source(db, source_id, principal.tenant_id)
    removal = await tombstone_source(
        db,
        tenant_id=principal.tenant_id,
        data_source=ds,
        now=dt.datetime.now(tz=dt.UTC),
        principal=principal,
    )
    await db.commit()
    await _purge_department_cache(principal.tenant_id)
    return SourceRemovalDTO(
        source_id=str(source_id),
        documents=removal.documents,
        chunks=removal.chunks,
        sha256=removal.sha256,
        audit_seq=removal.audit_seq,
    )


@router.delete(
    "/knowledge/bases/{kb_id}/sources/{source_id}",
    response_model=SourceUnlinkedFromBaseDTO,
    dependencies=[Depends(require_permission(perm(KNOWLEDGE, MANAGE)))],
)
async def unlink_source(
    kb_id: uuid.UUID,
    source_id: uuid.UUID,
    db: DbSession,
    principal: CurrentPrincipal,
) -> SourceUnlinkedFromBaseDTO:
    """Remove one source's content from one base -- the reverse of
    `POST /knowledge/sources/{id}/sync`'s `kbId` body param, which is today
    the only way a source is ever added to a base. Neither the source nor
    the base is deleted; both remain usable everywhere else they already
    were. A no-op (200, all-zero receipt) if this pair was never linked or
    was already unlinked -- not a 404 -- an operator retry or double-click
    must not surface as an error for an action that already succeeded."""
    kb = await _live_base(db, kb_id)
    ds = await _live_source(db, source_id, principal.tenant_id)
    removal = await unlink_source_from_base(
        db,
        tenant_id=principal.tenant_id,
        kb=kb,
        data_source=ds,
        now=dt.datetime.now(tz=dt.UTC),
        principal=principal,
    )
    await db.commit()
    await _purge_department_cache(principal.tenant_id)
    return SourceUnlinkedFromBaseDTO(
        kb_id=str(kb_id),
        data_source_id=str(source_id),
        documents=removal.documents,
        chunks=removal.chunks,
        sha256=removal.sha256,
        audit_seq=removal.audit_seq,
    )


@router.delete(
    "/knowledge/bases/{kb_id}",
    response_model=BaseRemovalDTO,
    dependencies=[Depends(require_permission(perm(KNOWLEDGE, MANAGE)))],
)
async def delete_base(
    kb_id: uuid.UUID,
    db: DbSession,
    principal: CurrentPrincipal,
) -> BaseRemovalDTO:
    """Erase a whole knowledge base, whichever sources fed it. Its grants go with
    it: a grant is an access rule, not a record, and `_grants_for` reads every
    grant unconditionally, so a dangling one is an authz hazard."""
    kb = await _live_base(db, kb_id)
    removal = await tombstone_base(
        db,
        tenant_id=principal.tenant_id,
        kb=kb,
        now=dt.datetime.now(tz=dt.UTC),
        principal=principal,
    )
    await db.commit()
    await _purge_department_cache(principal.tenant_id)
    return BaseRemovalDTO(
        kb_id=str(kb_id),
        documents=removal.documents,
        chunks=removal.chunks,
        sha256=removal.sha256,
        audit_seq=removal.audit_seq,
    )


@router.get(
    "/knowledge/jobs/{job_id}",
    response_model=IngestionJobDTO,
    dependencies=[Depends(require_permission(perm(KNOWLEDGE, VIEW)))],
)
async def get_ingestion_job(
    job_id: uuid.UUID, db: DbSession, principal: CurrentPrincipal
) -> IngestionJobDTO:
    job = await db.get(m.IngestionJob, job_id)
    if job is None or job.tenant_id != principal.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ingestion job not found")
    return ingestion_job_to_dto(job)


@router.post(
    "/knowledge/grants",
    response_model=GrantDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(KNOWLEDGE, MANAGE)))],
)
async def create_grant(
    body: CreateGrantRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> GrantDTO:
    # `_live_base`, not a bare `db.get`: `tombstone_base` hard-DELETEs a deleted
    # base's grants precisely because a dangling grant is an authz hazard, and
    # this route was measured re-creating them on the corpse -- 201, on a base
    # nothing else in the product will admit exists.
    await _live_base(db, body.kb_id)
    grantee: m.Department | m.Agent | None
    if body.grantee_type == "department":
        grantee = await db.get(m.Department, body.grantee_id)
    else:
        grantee = await db.get(m.Agent, body.grantee_id)
    if grantee is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{body.grantee_type} not found")

    existing = (
        await db.execute(
            select(m.KnowledgeGrant).where(
                m.KnowledgeGrant.kb_id == body.kb_id,
                m.KnowledgeGrant.grantee_type == body.grantee_type,
                m.KnowledgeGrant.grantee_id == body.grantee_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return GrantDTO(
            id=str(existing.id),
            kb_id=str(existing.kb_id),
            grantee_type=existing.grantee_type,
            grantee_id=str(existing.grantee_id),
        )

    grant = m.KnowledgeGrant(
        tenant_id=principal.tenant_id,
        kb_id=body.kb_id,
        grantee_type=body.grantee_type,
        grantee_id=body.grantee_id,
    )
    db.add(grant)
    await db.flush()
    return GrantDTO(
        id=str(grant.id),
        kb_id=str(grant.kb_id),
        grantee_type=grant.grantee_type,
        grantee_id=str(grant.grantee_id),
    )
