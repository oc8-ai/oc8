"""Browse a connected data source and fetch one file from it (spec §5.4).

These are pure service functions: they do NOT decide who may browse which
source. Authorization belongs to the API layer that calls them.

Guards applied here, whatever the connector does:

* `path` may not contain `..` segments or control characters, and `file_id` may
  not contain control characters or be oversized. A `file_id` is otherwise
  opaque (API-backed connectors use vendor ids) and is passed through untouched.
* Listings are capped at `MAX_BROWSE_ENTRIES`.
* A fetched file larger than `MAX_FETCH_BYTES` is refused, never truncated.
* A connector failure surfaces as a `ConnectorError` that names only the
  exception type, never its message: connector errors can echo URLs, tokens or
  config values.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.knowledge.connectors.base import BrowseEntry, ConnectorError, FetchedFile
from oc8.knowledge.connectors.context import SourceAuthContext
from oc8.knowledge.connectors.registry import resolve_connector

logger = logging.getLogger(__name__)

MAX_BROWSE_ENTRIES = 500
MAX_FETCH_BYTES = 10 * 1024 * 1024
_MAX_REF_LEN = 2048


class BrowseUnsupported(Exception):
    """This source's connector cannot browse; pin the source as a whole."""


def _auth(db: AsyncSession, source: m.DataSource) -> SourceAuthContext:
    return SourceAuthContext(
        db, tenant_id=source.tenant_id, connection_id=source.oauth_connection_id
    )


def _has_control_chars(value: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in value)


def _check_path(path: str) -> str:
    path = path or "/"
    if len(path) > _MAX_REF_LEN or _has_control_chars(path):
        raise ConnectorError("invalid browse path")
    if ".." in path.replace("\\", "/").split("/"):
        raise ConnectorError("invalid browse path: '..' segments are not allowed")
    return path


def _check_file_id(file_id: str) -> str:
    if not file_id or len(file_id) > _MAX_REF_LEN or _has_control_chars(file_id):
        raise ConnectorError("invalid file id")
    return file_id


async def _call(source: m.DataSource, what: str, coro: Any) -> Any:
    try:
        return await coro
    except ConnectorError:
        raise
    except Exception as exc:
        logger.warning(
            "connector %s failed to %s: %s", source.connector_type, what, type(exc).__name__
        )
        raise ConnectorError(f"the source could not {what} ({type(exc).__name__})") from None


async def browse_source(db: AsyncSession, *, source: m.DataSource, path: str) -> list[BrowseEntry]:
    path = _check_path(path)
    connector = await resolve_connector(
        db, tenant_id=source.tenant_id, type_id=source.connector_type
    )
    browse = getattr(connector, "browse", None)
    if browse is None:
        raise BrowseUnsupported(source.connector_type)
    entries: list[BrowseEntry] = await _call(
        source, "list files", browse(dict(source.config or {}), path, _auth(db, source))
    )
    return entries[:MAX_BROWSE_ENTRIES]


async def fetch_source_file(db: AsyncSession, *, source: m.DataSource, file_id: str) -> FetchedFile:
    file_id = _check_file_id(file_id)
    connector = await resolve_connector(
        db, tenant_id=source.tenant_id, type_id=source.connector_type
    )
    fetch_file = getattr(connector, "fetch_file", None)
    if fetch_file is None:
        raise BrowseUnsupported(source.connector_type)
    fetched: FetchedFile = await _call(
        source, "fetch the file", fetch_file(dict(source.config or {}), file_id, _auth(db, source))
    )
    if len(fetched.data) > MAX_FETCH_BYTES:
        raise ConnectorError(
            f"file is too large to pin ({len(fetched.data)} bytes; limit {MAX_FETCH_BYTES})"
        )
    return fetched
