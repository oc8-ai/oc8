"""§14.2 CloudEvents-compatible event envelope builder (pure)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from oc8.db.base import uuid7


def build_envelope(
    tenant_id: uuid.UUID,
    type_: str,
    data: dict[str, Any],
    source: str,
    *,
    audience: Sequence[uuid.UUID] | None = None,
) -> dict[str, Any]:
    """A §14.2 envelope. `id` (uuid7) is the client dedup key. `audience`
    restricts delivery to those members' sockets (see ConnectionManager); it
    is stripped before a frame reaches a browser."""
    env: dict[str, Any] = {
        "specversion": "1.0",
        "id": str(uuid7()),
        "source": source,
        "type": type_,
        "time": datetime.now(tz=UTC).isoformat(),
        "tenantid": str(tenant_id),
        "data": data,
    }
    if audience is not None:
        env["audience"] = [str(member_id) for member_id in audience]
    return env
