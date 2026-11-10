"""Browse a connected data source and fetch one file from it (spec §5.4).

These are pure service functions: they do NOT decide who may browse which
source. Authorization belongs to the API layer that calls them.

Guards applied here, whatever the connector does:

* `path` may not contain `..` segments or control characters, and `file_id` may
  not contain control characters or be oversized. A `file_id` is otherwise
  opaque (API-backed connectors use vendor ids) and is passed through untouched.
* Listings are capped at `MAX_BROWSE_ENTRIES`.
* `fetch_file` is handed `MAX_FETCH_BYTES`; a known size over it is refused up
  front, and `data` and `text` are each re-checked after the call.
* A fetched file larger than `MAX_FETCH_BYTES` is refused, never truncated.
* A connector failure surfaces as a `ConnectorError` that names only the
  exception type, never its message: connector errors can echo URLs, tokens or
  config values. That includes a `ConnectorError` the connector raised itself;
  only the messages written in this module ("too large", "invalid file id",
  ...) reach a caller verbatim.
* `fetch_source_file` fetches only a file the connector confirms is inside the
  source's configured scope (`in_scope`, required -- see `BrowsingConnector`).
  A connector without `in_scope` fetches nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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


@dataclass(frozen=True)
class BrowseListing:
    entries: list[BrowseEntry]
    #: True when the connector returned more than `MAX_BROWSE_ENTRIES`.
    truncated: bool


class BrowseUnsupported(Exception):
    """This source's connector cannot browse; pin the source as a whole."""


class InvalidReference(ConnectorError):
    """The caller's path or file id was refused before any connector saw it --
    the caller's mistake, not the source's."""


def _auth(db: AsyncSession, source: m.DataSource) -> SourceAuthContext:
    return SourceAuthContext(
        db, tenant_id=source.tenant_id, connection_id=source.oauth_connection_id
    )


def _has_control_chars(value: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in value)


def _check_path(path: str) -> str:
    path = path or "/"
    if len(path) > _MAX_REF_LEN or _has_control_chars(path):
        raise InvalidReference("invalid browse path")
    if ".." in path.replace("\\", "/").split("/"):
        raise InvalidReference("invalid browse path: '..' segments are not allowed")
    return path


def check_file_id(file_id: str) -> str:
    """`file_id`, or `ConnectorError` if it is empty, oversized or holds
    control characters."""
    if not file_id or len(file_id) > _MAX_REF_LEN or _has_control_chars(file_id):
        raise InvalidReference("invalid file id")
    return file_id


_NOT_IN_SCOPE = "the file is not part of this source"


def _too_large(size: int) -> str:
    return f"file is too large to pin ({size} bytes; limit {MAX_FETCH_BYTES})"


async def _call(source: m.DataSource, what: str, coro: Any) -> Any:
    try:
        return await coro
    except Exception as exc:
        # A connector's own ConnectorError is treated like any other failure:
        # its message is the connector's, not ours, and may echo config.
        status = getattr(exc, "status_code", None) or getattr(
            getattr(exc, "response", None), "status_code", None
        )
        logger.warning(
            "connector %s failed to %s (source=%s tenant=%s status=%s): %s",
            source.connector_type,
            what,
            source.id,
            source.tenant_id,
            status if isinstance(status, int) else "-",
            type(exc).__name__,
        )
        raise ConnectorError(f"the source could not {what} ({type(exc).__name__})") from None


async def browse_source(db: AsyncSession, *, source: m.DataSource, path: str) -> BrowseListing:
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
    return BrowseListing(
        entries=entries[:MAX_BROWSE_ENTRIES], truncated=len(entries) > MAX_BROWSE_ENTRIES
    )


async def fetch_source_file(
    db: AsyncSession, *, source: m.DataSource, file_id: str, known_size: int | None = None
) -> FetchedFile:
    file_id = check_file_id(file_id)
    if known_size is not None and known_size > MAX_FETCH_BYTES:
        raise ConnectorError(_too_large(known_size))
    connector = await resolve_connector(
        db, tenant_id=source.tenant_id, type_id=source.connector_type
    )
    fetch_file = getattr(connector, "fetch_file", None)
    if fetch_file is None:
        raise BrowseUnsupported(source.connector_type)
    if not await _in_scope(connector, source, file_id, db):
        raise ConnectorError(_NOT_IN_SCOPE)
    fetched: FetchedFile = await _call(
        source,
        "fetch the file",
        fetch_file(dict(source.config or {}), file_id, MAX_FETCH_BYTES, _auth(db, source)),
    )
    # Defence in depth: connectors must honour max_bytes themselves.
    size = max(len(fetched.data), len(fetched.text.encode()))
    if size > MAX_FETCH_BYTES:
        raise ConnectorError(_too_large(size))
    return fetched


async def _in_scope(connector: Any, source: m.DataSource, file_id: str, db: AsyncSession) -> bool:
    """Fails closed: no `in_scope`, a failure, or anything but `True` is "no"."""
    in_scope = getattr(connector, "in_scope", None)
    if in_scope is None:
        return False
    try:
        answer = await _call(
            source,
            "check the file",
            in_scope(dict(source.config or {}), file_id, _auth(db, source)),
        )
    except ConnectorError:
        return False
    return answer is True


async def source_file_in_scope(db: AsyncSession, *, source: m.DataSource, file_id: str) -> bool:
    """Does `source` actually expose `file_id`? False for an invalid id, a
    connector that cannot browse or cannot answer, and any failure -- never
    raises, so a caller can drop the ref without telling the reasons apart."""
    try:
        file_id = check_file_id(file_id)
        connector = await resolve_connector(
            db, tenant_id=source.tenant_id, type_id=source.connector_type
        )
    except ConnectorError:
        return False
    if getattr(connector, "fetch_file", None) is None:
        return False
    return await _in_scope(connector, source, file_id, db)
