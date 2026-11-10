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
    source_file_in_scope,
)
from oc8.knowledge.connectors.base import BrowseEntry, ConnectorError, FetchedFile
from oc8.knowledge.connectors.upload import UploadConnector
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class Browsing:
    type_id = "fake_drive"

    async def browse(self, config: dict[str, Any], path: str, auth: Any) -> list[BrowseEntry]:
        return [BrowseEntry(id="f1", name="Q3.xlsx", is_folder=False, size=10)]

    async def fetch_file(
        self, config: dict[str, Any], file_id: str, max_bytes: int, auth: Any
    ) -> FetchedFile:
        return FetchedFile(name="Q3.xlsx", content_type="text/csv", data=b"a,b", text="a,b")

    async def in_scope(self, config: dict[str, Any], file_id: str, auth: Any) -> bool:
        return True


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
            entries = (await browse_source(db, source=src, path="/")).entries
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

    async def fetch_file(
        self, config: dict[str, Any], file_id: str, max_bytes: int, auth: Any
    ) -> FetchedFile:
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
            listing = await browse_source(db, source=src, path="/")
            assert len(listing.entries) == MAX_BROWSE_ENTRIES and listing.truncated
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
    assert (await up.fetch_file(cfg, "a.txt", 100, None)).text == "hello"
    with pytest.raises(ConnectorError):
        await up.fetch_file(cfg, "b.txt", 100, None)


class Boom(Browsing):
    async def fetch_file(
        self, config: dict[str, Any], file_id: str, max_bytes: int, auth: Any
    ) -> FetchedFile:
        raise RuntimeError("token=hunter2")


class Recorder(Browsing):
    got: int | None = None

    async def fetch_file(
        self, config: dict[str, Any], file_id: str, max_bytes: int, auth: Any
    ) -> FetchedFile:
        self.got = max_bytes
        return FetchedFile(name="x", content_type="t", data=b"", text="z" * (max_bytes + 1))


async def test_fetch_failures_and_limits(
    app_session: AppSessionFactory, caplog: pytest.LogCaptureFixture
) -> None:
    tenant = uuid.uuid4()
    sid = await _source(app_session, tenant)
    async with app_session(tenant) as db:
        src = await db.get(m.DataSource, sid)
        assert src is not None
        with patch("oc8.knowledge.browse.resolve_connector", return_value=Boom()):
            with caplog.at_level("WARNING"), pytest.raises(ConnectorError) as ei:
                await fetch_source_file(db, source=src, file_id="f")
            assert "hunter2" not in str(ei.value) and "hunter2" not in caplog.text
            assert str(sid) in caplog.text and "Traceback" not in caplog.text
            for bad in ("x" * 5000, "a\nb"):
                with pytest.raises(ConnectorError, match="invalid file id"):
                    await fetch_source_file(db, source=src, file_id=bad)
        rec = Recorder()
        with patch("oc8.knowledge.browse.resolve_connector", return_value=rec):
            with pytest.raises(ConnectorError, match="too large"):
                await fetch_source_file(db, source=src, file_id="f")
            assert rec.got == MAX_FETCH_BYTES
            rec.got = None
            with pytest.raises(ConnectorError, match="too large"):
                await fetch_source_file(db, source=src, file_id="f", known_size=MAX_FETCH_BYTES + 1)
            assert rec.got is None


async def test_upload_edge_cases() -> None:
    up = UploadConnector()
    assert await up.browse({"filename": "", "content": "x"}, "/", None) == []
    cfg = {"filename": "a.txt", "content": "hello", "content_type": "text/plain"}
    with pytest.raises(ConnectorError, match="too large"):
        await up.fetch_file(cfg, "a.txt", 4, None)
    with pytest.raises(ConnectorError):
        await up.fetch_file({"filename": "", "content": "x"}, "", 10, None)


class NoScopeCheck(Browsing):
    in_scope = None  # type: ignore[assignment]


class OnlyF1(Browsing):
    async def in_scope(self, config: dict[str, Any], file_id: str, auth: Any) -> bool:
        return file_id == "f1"


class ScopeBoom(Browsing):
    async def in_scope(self, config: dict[str, Any], file_id: str, auth: Any) -> bool:
        raise RuntimeError("token=hunter2")


async def test_only_a_file_inside_the_sources_scope_is_fetched(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    sid = await _source(app_session, tenant)
    async with app_session(tenant) as db:
        src = await db.get(m.DataSource, sid)
        assert src is not None
        with patch("oc8.knowledge.browse.resolve_connector", return_value=OnlyF1()):
            assert await source_file_in_scope(db, source=src, file_id="f1")
            assert not await source_file_in_scope(db, source=src, file_id="elsewhere")
            assert not await source_file_in_scope(db, source=src, file_id="a\nb")
            assert not await source_file_in_scope(db, source=src, file_id="")
            assert (await fetch_source_file(db, source=src, file_id="f1")).text == "a,b"
            with pytest.raises(ConnectorError, match="not part of this source"):
                await fetch_source_file(db, source=src, file_id="elsewhere")
        for connector in (NoScopeCheck(), ScopeBoom(), Plain()):
            with patch("oc8.knowledge.browse.resolve_connector", return_value=connector):
                assert not await source_file_in_scope(db, source=src, file_id="f1")
        with patch("oc8.knowledge.browse.resolve_connector", return_value=NoScopeCheck()):
            with pytest.raises(ConnectorError, match="not part of this source"):
                await fetch_source_file(db, source=src, file_id="f1")
        with patch("oc8.knowledge.browse.resolve_connector", side_effect=ConnectorError("unknown")):
            assert not await source_file_in_scope(db, source=src, file_id="f1")


class OwnError(Browsing):
    async def fetch_file(
        self, config: dict[str, Any], file_id: str, max_bytes: int, auth: Any
    ) -> FetchedFile:
        raise ConnectorError("cannot reach https://u:hunter2@host/drive")


async def test_a_connectors_own_error_message_is_not_passed_through(
    app_session: AppSessionFactory, caplog: pytest.LogCaptureFixture
) -> None:
    tenant = uuid.uuid4()
    sid = await _source(app_session, tenant)
    async with app_session(tenant) as db:
        src = await db.get(m.DataSource, sid)
        assert src is not None
        with patch("oc8.knowledge.browse.resolve_connector", return_value=OwnError()):
            with caplog.at_level("WARNING"), pytest.raises(ConnectorError) as ei:
                await fetch_source_file(db, source=src, file_id="f1")
    assert str(ei.value) == "the source could not fetch the file (ConnectorError)"
    assert "hunter2" not in caplog.text


async def test_upload_connector_scope() -> None:
    cfg = {"filename": "a.txt", "content": "hello"}
    up = UploadConnector()
    assert await up.in_scope(cfg, "a.txt", None)
    assert not await up.in_scope(cfg, "b.txt", None)
    assert not await up.in_scope({"filename": ""}, "", None)
