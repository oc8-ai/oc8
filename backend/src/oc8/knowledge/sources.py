"""Proving a data source's configuration before anything persists it (§11.2).

`POST /knowledge/sources` used to be the only way to make a source, so it was
also the only place that ran the two checks a source needs: `resolve_connector`
(is this connector type real, and may THIS tenant use it?) and the connector's
own `validate()` (does this configuration actually work, with these
credentials?). The plugin setup form's `oauth_provision` block then grew a
second, quieter way in -- an ORM row built by hand -- which ran neither. A
typo'd `connector_type` in a manifest became a dead row nobody noticed until
the first sync, and for Microsoft 365 the `GET /organization` call that proves
the Azure app's Graph permissions were actually admin-consented never ran at
all: minting a token proves the client secret and nothing else, and an app
registration with no consented permissions mints one happily and then 403s on
every real call.

One function, called by both writers. Read-only: it persists nothing and the
caller still owns both the row and the transaction.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oc8.knowledge.connectors.base import Connector
from oc8.knowledge.connectors.context import SourceAuthContext
from oc8.knowledge.connectors.registry import resolve_connector
from oc8.knowledge.scope_prune import empty_pickers


class SourceRejected(Exception):
    """The connector refused this configuration. Carries the connector's own
    words -- an operator can act on "configure at least one site or drive ID"
    and cannot act on "invalid"."""


async def validate_source_config(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connector_type: str,
    config: dict[str, Any],
    oauth_connection_id: uuid.UUID | None,
) -> Connector:
    """The connector this source would use, having proven it accepts `config`.

    Raises `ConnectorError` for an unknown or unavailable connector type and
    `SourceRejected` when the connector itself says no.
    """
    connector = await resolve_connector(db, tenant_id=tenant_id, type_id=connector_type)
    # Generic, off the `x-oc8-picker` schema marker, and before the connector is
    # asked anything: a source that selects nothing is a source to delete, and
    # refusing it everywhere is what lets a sync after a scope change read an
    # empty authoritative listing as the truth (oc8/knowledge/scope_prune.py).
    empty = empty_pickers(getattr(connector, "config_schema", None), config)
    if empty:
        raise SourceRejected(
            f"select at least one item for {', '.join(empty)}; "
            "to stop indexing a source entirely, delete it"
        )
    # validate() must see the credentials, or a connector that needs any
    # would reject every source at creation time.
    result = await connector.validate(
        config,
        SourceAuthContext(db, tenant_id=tenant_id, connection_id=oauth_connection_id),
    )
    if not result.ok:
        raise SourceRejected(result.error or "invalid config")
    if connector.requires_oauth is not None and oauth_connection_id is None:
        raise SourceRejected(f"connector {connector_type!r} requires an oauth connection")
    return connector
