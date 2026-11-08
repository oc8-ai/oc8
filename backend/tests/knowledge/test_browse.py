from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import patch

import pytest

from oc8 import models as m
from oc8.knowledge.browse import (
    MAX_BROWSE_ENTRIES,
    MAX_FETCH_BYTES,
    BrowseUnsupported,
    browse_source,
    fetch_source_file,
)
from oc8.knowledge.connectors.base import BrowseEntry, ConnectorError, FetchedFile
from oc8.knowledge.connectors.upload import UploadConnector
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class Browsing:
    type_id = "fake_drive"

    async def browse(self, config: dict[str, Any], path: str, auth: Any) -> list[BrowseEntry]:
        return [BrowseEntry(id="f1", name="Q3.xlsx", is_folder=False, size=10)]

    async def fetch_file(self, config: dict[str, Any], file_id: str, auth: Any) -> FetchedFile:
        return FetchedFile(name="Q3.xlsx", content_type="text/csv", data=b"a,b", text="a,b")


class Plain:
    type_id = "fake_site"


async def _source(app_session: AppSessionFactory, tenant: uuid.UUID) -> uuid.UUID:
    async with app_session(tenant) as db:
        src = m.DataSource(tenant_id=tenant, connector_type="fake_drive", name="Drive", config={})
        db.add(src)
        await db.flush()
        return src.id


async def test_a_browsing_connector_lists_and_fetches(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    sid = await _source(app_session, tenant)
    with patch("oc8.knowledge.browse.resolve_connector", return_value=Browsing()):
        async with app_session(tenant) as db:
            src = await db.get(m.DataSource, sid)
            assert src is not None
            entries = await browse_source(db, source=src, path="/")
            fetched = await fetch_source_file(db, source=src, file_id="f1")
    assert entries[0].name == "Q3.xlsx" and fetched.text == "a,b"


async def test_a_connector_without_browse_degrades(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    sid = await _source(app_session, tenant)
    with patch("oc8.knowledge.browse.resolve_connector", return_value=Plain()):
        async with app_session(tenant) as db:
            src = await db.get(m.DataSource, sid)
            assert src is not None
            with pytest.raises(BrowseUnsupported):
                await browse_source(db, source=src, path="/")
            with pytest.raises(BrowseUnsupported):
                await fetch_source_file(db, source=src, file_id="f1")


class Big(Browsing):
    async def browse(self, config: dict[str, Any], path: str, auth: Any) -> list[BrowseEntry]:
        return [
            BrowseEntry(id=str(i), name=str(i), is_folder=False)
            for i in range(MAX_BROWSE_ENTRIES + 5)
        ]

    async def fetch_file(self, config: dict[str, Any], file_id: str, auth: Any) -> FetchedFile:
        return FetchedFile(
            name="x", content_type="text/plain", data=b"x" * (MAX_FETCH_BYTES + 1), text=""
        )


class Leaky(Browsing):
    async def browse(self, config: dict[str, Any], path: str, auth: Any) -> list[BrowseEntry]:
        raise RuntimeError("401 for https://u:hunter2@host")


async def test_guards(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    sid = await _source(app_session, tenant)
    async with app_session(tenant) as db:
        src = await db.get(m.DataSource, sid)
        assert src is not None
        with patch("oc8.knowledge.browse.resolve_connector", return_value=Big()):
            assert len(await browse_source(db, source=src, path="/")) == MAX_BROWSE_ENTRIES
            with pytest.raises(ConnectorError, match="too large"):
                await fetch_source_file(db, source=src, file_id="f")
            for bad in ("../etc", "a/../../b", "a\\..\\b", "a\x00b"):
                with pytest.raises(ConnectorError):
                    await browse_source(db, source=src, path=bad)
            with pytest.raises(ConnectorError):
                await fetch_source_file(db, source=src, file_id="")
        with patch("oc8.knowledge.browse.resolve_connector", return_value=Leaky()):
            with pytest.raises(ConnectorError) as ei:
                await browse_source(db, source=src, path="/")
            assert "hunter2" not in str(ei.value)


async def test_upload_connector_browses() -> None:
    cfg = {"filename": "a.txt", "content": "hello", "content_type": "text/plain"}
    up = UploadConnector()
    entries = await up.browse(cfg, "/", None)
    assert [e.id for e in entries] == ["a.txt"] and entries[0].size == 5
    assert await up.browse(cfg, "/sub", None) == []
    assert (await up.fetch_file(cfg, "a.txt", None)).text == "hello"
    with pytest.raises(ConnectorError):
        await up.fetch_file(cfg, "b.txt", None)
