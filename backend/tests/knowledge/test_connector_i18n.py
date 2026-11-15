"""A plugin connector's catalog entry and its picker's app entries carry the
translations of their capa's `i18n/*.po` catalogs, the same way capa labels
and setup fields do (`*Translations`, resolved by the frontend per locale)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.capas.deferred_capa import NAME, TYPE_ID, reset, use_capas_path, write_capa
from tests.conftest import AppSessionFactory
from tests.connections.conftest import _PLUGINS_DIR
from tests.connections.helpers import api, headers

pytestmark = pytest.mark.asyncio

_CONNECTOR = """
from __future__ import annotations

from typing import Any

from oc8.knowledge.connectors.base import BrowseEntry, ValidationResult


class DemoConnector:
    type_id = "deferred_demo"
    label = "Demo (Files & Notes)"
    description = "Files and notes"
    config_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "selection": {"type": "array", "title": "Folders", "description": "What to read"},
            "name": {"type": "string", "title": "Untranslated"},
        },
    }
    requires_oauth: str | None = None

    async def validate(self, config, auth=None):
        return ValidationResult(ok=True)

    async def discover(self, config, auth=None):
        return []

    async def fetch(self, config, cursor, auth=None):
        if False:
            yield None

    async def browse_selectable(self, config, path, auth=None):
        return [
            BrowseEntry(id="app:files", name="Files", is_folder=True, kind="app"),
            BrowseEntry(id="folder:1", name="Files", is_folder=True, kind="folder"),
        ]


def register(contrib: Any) -> None:
    contrib.add_connector(DemoConnector())
"""

_DE_PO = """msgid ""
msgstr "Content-Type: text/plain; charset=UTF-8\\n"

msgid "Demo (Files & Notes)"
msgstr "Demo (Dateien & Notizen)"

msgid "Files and notes"
msgstr "Dateien und Notizen"

msgid "Folders"
msgstr "Ordner"

msgid "What to read"
msgstr "Was gelesen wird"

msgid "Files"
msgstr "Dateien"
"""


@pytest.fixture
def capas_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    use_capas_path(tmp_path, monkeypatch)
    write_capa(tmp_path, permissions=["knowledge:write"])
    (tmp_path / NAME / "connector" / "connector.py").write_text(_CONNECTOR)
    (tmp_path / NAME / "i18n").mkdir()
    (tmp_path / NAME / "i18n" / "de.po").write_text(_DE_PO)
    yield tmp_path
    reset()


async def test_catalog_carries_the_capa_translations(
    app_session: AppSessionFactory, capas_root: Path
) -> None:
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    async with api() as c:
        r = await c.post("/api/v1/capas/install-from-disk", json={"pluginId": NAME}, headers=admin)
        assert r.status_code == 201, r.text
        listed = (await c.get("/api/v1/knowledge/connectors", headers=admin)).json()
    row = next(x for x in listed if x["typeId"] == TYPE_ID)
    assert row["label"] == "Demo (Files & Notes)"
    assert row["labelTranslations"] == {"de": "Demo (Dateien & Notizen)"}
    assert row["descriptionTranslations"] == {"de": "Dateien und Notizen"}
    assert row["fieldTranslations"] == {
        "selection": {"title": {"de": "Ordner"}, "description": {"de": "Was gelesen wird"}}
    }
    # A core connector has no capa catalog.
    website = next(x for x in listed if x["typeId"] == "website")
    assert website["labelTranslations"] == {}
    assert website["fieldTranslations"] == {}


async def test_picker_app_entries_carry_the_capa_translations(
    app_session: AppSessionFactory, capas_root: Path
) -> None:
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    async with api() as c:
        r = await c.post("/api/v1/capas/install-from-disk", json={"pluginId": NAME}, headers=admin)
        assert r.status_code == 201, r.text
        b = await c.post(
            f"/api/v1/knowledge/connectors/{TYPE_ID}/browse",
            json={"config": {}, "path": "/"},
            headers=admin,
        )
    assert b.status_code == 200, b.text
    app, folder = b.json()["entries"]
    assert app["nameTranslations"] == {"de": "Dateien"}
    # Only an app's name is the connector's own text; a folder's is the user's.
    assert folder["nameTranslations"] == {}


@pytest.fixture
def real_capas(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    use_capas_path(_PLUGINS_DIR, monkeypatch)
    yield
    reset()


async def test_odoo_knowledge_is_german_in_the_catalog(
    app_session: AppSessionFactory, real_capas: None
) -> None:
    """The real odoo_mcp capa, while its knowledge connector waits for consent."""
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    async with api() as c:
        r = await c.post(
            "/api/v1/capas/install-from-disk", json={"pluginId": "odoo_mcp"}, headers=admin
        )
        assert r.status_code in (200, 201), r.text
        listed = (await c.get("/api/v1/knowledge/connectors", headers=admin)).json()
    row = next(x for x in listed if x["typeId"] == "odoo_knowledge")
    assert row["labelTranslations"]["de"] == "Odoo (Dokumente & Wissen)"
    assert row["fieldTranslations"]["credential"]["title"]["de"] == "Odoo-Zugang"
    assert row["fieldTranslations"]["selection"]["title"]["de"] == "Ordner und Artikel"
