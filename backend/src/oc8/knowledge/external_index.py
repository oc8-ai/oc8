"""Helpers for attaching an external vector index to a Knowledge Base."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.credentials.service import CredentialNotFound, get_credential
from oc8.knowledge.connectors.context import SourceAuthContext
from oc8.knowledge.vector_indexes.base import VectorIndexError
from oc8.knowledge.vector_indexes.registry import INTERNAL_INDEX_TYPE, resolve_vector_index


class ExternalIndexRejected(Exception):
    """Operator-facing reason the external index cannot be attached."""


def is_external_index(kb: m.KnowledgeBase) -> bool:
    return (kb.index_type or INTERNAL_INDEX_TYPE) != INTERNAL_INDEX_TYPE


async def validate_external_index_binding(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    index_type: str,
    index_config: dict[str, Any],
    credential_id: uuid.UUID | None,
) -> None:
    """Resolve the capa, check the credential type, and probe the store.

    Raises ``ExternalIndexRejected`` with an operator-facing message.
    """
    if index_type == INTERNAL_INDEX_TYPE:
        if credential_id is not None:
            raise ExternalIndexRejected(
                "internal knowledge bases do not take a credential_id"
            )
        return

    if credential_id is None:
        raise ExternalIndexRejected(
            "an external index requires credentialId — create one under Credentials first"
        )

    # Secrets must never appear in index_config.
    for key in index_config:
        lowered = str(key).lower()
        if any(s in lowered for s in ("password", "api_key", "apikey", "secret", "token", "dsn")):
            raise ExternalIndexRejected(
                f"indexConfig must not contain secrets (refusing key {key!r}); "
                "use a Credential instead"
            )

    try:
        index = await resolve_vector_index(db, tenant_id=tenant_id, type_id=index_type)
    except VectorIndexError as exc:
        raise ExternalIndexRejected(str(exc)) from exc

    try:
        cred = await get_credential(db, tenant_id=tenant_id, credential_id=credential_id)
    except CredentialNotFound as exc:
        raise ExternalIndexRejected(f"credential not found: {credential_id}") from exc

    if cred.credential_type != index.credential_type:
        raise ExternalIndexRejected(
            f"credential type {cred.credential_type!r} does not match "
            f"index type {index_type!r} (expects {index.credential_type!r})"
        )

    auth = SourceAuthContext(db, tenant_id=tenant_id)
    try:
        await index.validate(index_config, auth, credential_id=str(credential_id))
    except VectorIndexError as exc:
        raise ExternalIndexRejected(str(exc)) from exc
