"""GET /knowledge/connectors says whether each connector can be set up now: a
connector that signs in somewhere needs an active organisation connection of
that provider (a shared source must not run on one person's account)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from oc8.api.v1 import knowledge as knowledge_api
from tests.conftest import AppSessionFactory
from tests.connections.helpers import api, headers, member, oauth_row

pytestmark = pytest.mark.asyncio


@dataclass
class _Connector:
    requires_oauth: str | None
    label: str = "x"
    description: str = "x"
    config_schema: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.config_schema = self.config_schema or {"type": "object", "properties": {}}


async def test_connectors_say_whether_they_are_usable(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()

    async def _available(db: Any, *, tenant_id: uuid.UUID) -> dict[str, _Connector]:
        return {
            "website": _Connector(None),
            "google_workspace_files": _Connector("google"),
            "microsoft365_files": _Connector("microsoft"),
            "gdrive": _Connector("google"),
        }

    monkeypatch.setattr(knowledge_api, "available_connectors", _available)
    anna = await member(app_session, tenant, "anna@acme.test")
    # Microsoft: an organisation connection. Google: only Anna's own sign-in
    # and a revoked organisation one -- neither can feed a shared source.
    await oauth_row(app_session, tenant, owner=None, provider="microsoft")
    await oauth_row(app_session, tenant, owner=anna, provider="google")
    await oauth_row(app_session, tenant, owner=None, provider="google", status="revoked")
    async with api() as client:
        r = await client.get(
            "/api/v1/knowledge/connectors", headers=headers(tenant, "admin", "org_admin")
        )
    assert r.status_code == 200, r.text
    rows = {row["typeId"]: (row["usable"], row["missing"]) for row in r.json()}
    assert rows == {
        "website": (True, None),
        "microsoft365_files": (True, None),
        "google_workspace_files": (False, "oauth:google"),
        "gdrive": (False, "oauth:google"),
    }
