"""Browse a connector before a source exists: the config is a draft, nothing is stored."""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from oc8 import config
from oc8 import models as m
from oc8.capas.manifest import CredentialTypeSpec, SetupFieldSpec
from oc8.credentials import registry
from oc8.credentials.service import create_credential
from oc8.knowledge.connectors.base import BrowseEntry, ConnectorError
from tests.capas.deferred_capa import NAME, TYPE_ID, reset, use_capas_path, write_capa
from tests.conftest import AppSessionFactory
from tests.connections.helpers import api, headers

pytestmark = pytest.mark.asyncio

URL = "/api/v1/knowledge/connectors/{}/browse"
PATCH = "oc8.knowledge.browse.resolve_connector"


class Selectable:
    type_id = "fake_odoo"

    async def browse_selectable(
        self, config: dict[str, Any], path: str, auth: Any
    ) -> list[BrowseEntry]:
        return [
            BrowseEntry(id="ws1", name="Contracts", is_folder=True, kind="workspace"),
            BrowseEntry(
                id="a1", name="Handbook", is_folder=False, kind="article", note="12 pages"
            ),
        ]


class Many(Selectable):
    async def browse_selectable(
        self, config: dict[str, Any], path: str, auth: Any
    ) -> list[BrowseEntry]:
        return [BrowseEntry(id=str(i), name=str(i), is_folder=False) for i in range(600)]


class Leaky(Selectable):
    async def browse_selectable(
        self, config: dict[str, Any], path: str, auth: Any
    ) -> list[BrowseEntry]:
        raise ConnectorError("https://u:pw@x secret")


class UsesCredential(Selectable):
    """Reads the credential named in the config. A refusal is recorded and
    re-raised; a successful read leaks the secret into an entry name."""

    def __init__(self) -> None:
        self.refused = False

    async def browse_selectable(
        self, config: dict[str, Any], path: str, auth: Any
    ) -> list[BrowseEntry]:
        try:
            secret = await auth.credential(config["credentialId"], "api_key")
        except ConnectorError:
            self.refused = True
            raise
        return [BrowseEntry(id="x", name=secret, is_folder=False)]


@pytest.fixture
def _personal_type() -> Iterator[None]:
    registry.CORE_CREDENTIAL_TYPES["odoo_test"] = CredentialTypeSpec(
        name="odoo_test",
        display_name="Odoo",
        fields=[SetupFieldSpec(key="api_key", label="Key", kind="password", required=False)],
    )
    yield
    registry.CORE_CREDENTIAL_TYPES.pop("odoo_test", None)


async def _post(
    tenant: uuid.UUID,
    connector: Any,
    *,
    type_id: str = "fake_odoo",
    role: str = "org_admin",
    body: dict[str, Any] | None = None,
) -> Any:
    async with api() as c:
        with patch(PATCH, return_value=connector):
            return await c.post(
                URL.format(type_id),
                json=body or {"config": {}, "path": "/"},
                headers=headers(tenant, "u@acme.test", role),
            )


async def test_browse_draft_lists_entries_with_kind_and_note() -> None:
    r = await _post(uuid.uuid4(), Selectable())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["truncated"] is False
    assert body["entries"] == [
        {
            "id": "ws1",
            "name": "Contracts",
            "isFolder": True,
            "size": None,
            "contentType": None,
            "kind": "workspace",
            "note": None,
            "nameTranslations": {},
        },
        {
            "id": "a1",
            "name": "Handbook",
            "isFolder": False,
            "size": None,
            "contentType": None,
            "kind": "article",
            "note": "12 pages",
            "nameTranslations": {},
        },
    ]


async def test_browse_draft_requires_knowledge_manage() -> None:
    r = await _post(uuid.uuid4(), Selectable(), role="auditor")
    assert r.status_code == 403


async def test_browse_draft_unsupported_connector() -> None:
    async with api() as c:
        r = await c.post(
            URL.format("website"),
            json={"config": {"url": "https://example.com"}, "path": "/"},
            headers=headers(uuid.uuid4(), "a@acme.test", "org_admin"),
        )
    assert r.status_code == 400
    assert r.json()["detail"] == "This source can't be browsed."


async def test_browse_draft_unknown_type_is_404() -> None:
    async with api() as c:
        r = await c.post(
            URL.format("no_such_type"),
            json={"config": {}, "path": "/"},
            headers=headers(uuid.uuid4(), "a@acme.test", "org_admin"),
        )
    assert r.status_code == 404


async def test_browse_draft_scrubs_connector_errors() -> None:
    r = await _post(uuid.uuid4(), Leaky())
    assert r.status_code == 400
    assert r.json()["detail"] == "the source could not list files (ConnectorError)"


async def test_browse_draft_caps_entries() -> None:
    r = await _post(uuid.uuid4(), Many())
    assert r.status_code == 200
    assert len(r.json()["entries"]) == 500
    assert r.json()["truncated"] is True


async def test_browse_draft_rejects_dotdot_path() -> None:
    r = await _post(uuid.uuid4(), Selectable(), body={"config": {}, "path": "/a/../b"})
    assert r.status_code == 400


async def test_browse_draft_personal_credential_unusable(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, _personal_type: None
) -> None:
    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        member = m.OrgMember(tenant_id=tenant, subject="owner", subject_uuid=uuid.uuid4())
        db.add(member)
        await db.flush()
        cred = await create_credential(
            db,
            tenant_id=tenant,
            name="Mine",
            credential_type="odoo_test",
            field_values={"api_key": "PERSONAL_SECRET_VALUE"},
            owner_member_id=member.id,
        )
        cred_id = str(cred.id)
    connector = UsesCredential()
    r = await _post(tenant, connector, body={"config": {"credentialId": cred_id}, "path": "/"})
    assert r.status_code == 400
    assert connector.refused
    assert "PERSONAL_SECRET_VALUE" not in r.text


async def test_browse_draft_consent_pending_connector_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_capas_path(tmp_path, monkeypatch)
    try:
        tenant = uuid.uuid4()
        admin = headers(tenant, "admin@acme.test", "org_admin")
        write_capa(
            tmp_path,
            permissions=["mcp:connect", "knowledge:write"],
            deferred=["knowledge:write"],
        )
        async with api() as c:
            r = await c.post(
                "/api/v1/capas/install-from-disk", json={"pluginId": NAME}, headers=admin
            )
            assert r.status_code == 201, r.text
            r = await c.post(
                URL.format(TYPE_ID), json={"config": {}, "path": "/"}, headers=admin
            )
        assert r.status_code == 404
    finally:
        reset()
