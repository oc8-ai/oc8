"""A tool session whose account was resolved for reading only.

A Copilot direct-access bridge that serves every call of a run is resolved
for `modify` when the tool can write; when no department of the person
grants modify, it falls back to a login lent for reading (unified
connections, "Resolver order (Copilot direct access)"). That login must never
carry a write: this wrapper offers only the read tools and refuses any call
whose `required_right` is more than read.
"""

from __future__ import annotations

from typing import Any

from oc8 import models as m
from oc8.authz.pdp import required_right
from oc8.connections.access import ConnectionRequired, declared_scopes
from oc8.connections.integrations import integration_for_connection


class ReadOnlyBridge:
    """Wraps an unentered tool session (`async with` like the session)."""

    def __init__(self, inner: Any, conn: m.McpConnection) -> None:
        self._inner = inner
        self._conn = conn
        self._scopes = declared_scopes(conn)
        self._entered: Any = None

    def _reads(self, name: str) -> bool:
        return required_right(name, self._scopes) == "read"

    async def __aenter__(self) -> ReadOnlyBridge:
        self._entered = await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> Any:
        return await self._inner.__aexit__(*exc)

    @property
    def tools(self) -> list[Any]:
        return [t for t in self._entered.tools if self._reads(t.name)]

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        if not self._reads(name):
            integration = integration_for_connection(self._conn)
            if integration is not None:
                raise ConnectionRequired(integration, "read_only")
            raise PermissionError(
                f"the account of {self._conn.name!r} in this conversation may only read"
            )
        result: str = await self._entered.call(name, arguments)
        return result

    def __getattr__(self, name: str) -> Any:
        # Everything else (resources, prompts, ...) of the entered session.
        return getattr(self._entered, name)
