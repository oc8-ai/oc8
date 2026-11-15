"""The `odoo_knowledge` connector, against a fake Odoo.

The fake keeps Documents and Knowledge records in memory and answers both
protocols: `/jsonrpc` `execute_kw` (Odoo 17, folders in `documents.folder`)
and JSON-2 (Odoo 19, folders are `documents.document` records with
`type = 'folder'`). It understands just the domains the connector sends:
`=`, `!=`, `in` and `child_of`, ANDed. It is injected through the
connector's `transport`; the outbound guard still runs, so names resolve
through the patched resolver below.

Skips and notes are read the way core reads them: a collector installed
around the iteration of `fetch` (`oc8.knowledge.connectors.report`).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _evict() -> None:
    """`connector` is a package name several capas share, and sys.modules is
    keyed by name: drop any cached copy before and after the import below."""
    for _stale in [n for n in sys.modules if n == "connector" or n.startswith("connector.")]:
        del sys.modules[_stale]


_evict()
sys.path.insert(0, str(PLUGIN_ROOT))

from connector.connector import (  # noqa: E402
    MAX_BYTES,
    OdooKnowledgeConnector,
    register,
)
from connector.odoo_client import (  # noqa: E402
    OdooAccessDenied,
    OdooAppMissing,
    OdooLoginRefused,
    OdooUnreachable,
)

sys.path.remove(str(PLUGIN_ROOT))
_evict()

from oc8.knowledge.connectors.base import (  # noqa: E402
    Attestation,
    ConnectorError,
    RawDocument,
)
from oc8.knowledge.connectors.report import collect_sync_report  # noqa: E402

pytestmark = pytest.mark.asyncio

URL = "https://odoo.acme.test"
HOST = "odoo.acme.test"
DB = "acme-prod-db"
LOGIN = "knowledge-bot@acme.test"
SECRET = "sk-odoo-0123456789abcdef"
CREDENTIAL = "5f0c4c1e-1111-4222-8333-944445555666"
UID = 7
WRITE_DATE = "2026-11-01 10:00:00"

PDF = b"%PDF-1.4 fake pdf bytes"
TXT = b"Q1 numbers: all good."
MD = b"# HR notes\n\nBe kind."


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Names resolve to a public address; IP literals and localhost stay put."""
    import ipaddress

    def resolve(host: str) -> list[str]:
        if host == "localhost":
            return ["127.0.0.1"]
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return ["93.184.216.34"]
        return [host]

    monkeypatch.setattr("oc8.net.outbound.resolve_ips", resolve)


# ---- the fake Odoo -----------------------------------------------------------


class _Streamed(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aiter__(self) -> Any:
        yield self._data


def _streamed(handler: Any) -> httpx.MockTransport:
    def respond(request: httpx.Request) -> httpx.Response:
        response: httpx.Response = handler(request)
        headers = [(k, v) for k, v in response.headers.items() if k.lower() != "content-length"]
        return httpx.Response(
            response.status_code, headers=headers, stream=_Streamed(response.content)
        )

    return httpx.MockTransport(respond)


class _Fail(Exception):
    def __init__(self, name: str, message: str, status: int) -> None:
        self.name, self.message, self.status = name, message, status


#: Many2one fields per model, returned as [id, display name].
_M2O = {
    "documents.folder": {"parent_folder_id"},
    "documents.document": {"folder_id"},
    "knowledge.article": {"parent_id"},
}
#: The field that makes each model a tree.
_PARENT = {
    "documents.folder": "parent_folder_id",
    "documents.document": "folder_id",
    "knowledge.article": "parent_id",
}


def _file(rid: int, name: str, folder: int | None, mimetype: str, data: bytes, **kw: Any) -> Any:
    return {
        "id": rid,
        "name": name,
        "folder_id": folder,
        "type": kw.pop("type", "binary"),
        "mimetype": mimetype,
        "file_size": kw.pop("file_size", len(data)),
        "datas": base64.b64encode(data).decode() if data else False,
        "write_date": kw.pop("write_date", WRITE_DATE),
        **kw,
    }


class KnowledgeOdoo:
    """Just enough Odoo for Documents and Knowledge, in 17 or 19 shape.

    Folders: Finance (1) > Invoices (2), and HR (3). Files: report.pdf (10) in
    Finance, q1.txt (11) in Invoices, notes.md (12) in HR. Articles: Handbook
    (50) > Onboarding (51), Policies (53), and Old (52) in the trash.
    """

    def __init__(
        self,
        major: int = 19,
        *,
        json2_probe: int | None = None,
        authenticate: Any = UID,
        missing: tuple[str, ...] = (),
        denied: tuple[str, ...] = (),
        down: bool = False,
    ) -> None:
        self.major = major
        self.version = [major, 0, 0, "final", 0, ""]
        self.json2_probe = json2_probe
        self.authenticate = authenticate
        self.missing = set(missing)
        self.denied = set(denied)
        self.down = down
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        #: (model, id) whose `read` answers MissingError, as if deleted mid-sync.
        self.vanish_on_read: set[tuple[str, int]] = set()
        folders: dict[int, dict[str, Any]] = {
            1: {"id": 1, "name": "Finance", "parent": None},
            2: {"id": 2, "name": "Invoices", "parent": 1},
            3: {"id": 3, "name": "HR", "parent": None},
        }
        files = {
            10: _file(10, "report.pdf", 1, "application/pdf", PDF),
            11: _file(11, "q1.txt", 2, "text/plain", TXT),
            12: _file(12, "notes.md", 3, "text/markdown", MD),
        }
        if major < 18:
            self.records: dict[str, dict[int, dict[str, Any]]] = {
                "documents.folder": {
                    f["id"]: {"id": f["id"], "name": f["name"], "parent_folder_id": f["parent"]}
                    for f in folders.values()
                },
                "documents.document": files,
            }
        else:
            as_documents = {
                f["id"]: {
                    "id": f["id"],
                    "name": f["name"],
                    "folder_id": f["parent"],
                    "type": "folder",
                    "mimetype": False,
                    "file_size": 0,
                    "datas": False,
                    "write_date": WRITE_DATE,
                }
                for f in folders.values()
            }
            self.records = {"documents.document": {**as_documents, **files}}
            # Odoo 18+ has file shortcuts: a document pointing at another one.
            for document in self.records["documents.document"].values():
                document["shortcut_document_id"] = False
        self.records["knowledge.article"] = {
            50: self._article(50, "Handbook", None, "<p>Welcome to Acme.</p>"),
            51: self._article(51, "Onboarding", 50, "<p>Day one.</p>"),
            52: self._article(52, "Old", None, "<p>Gone.</p>", to_delete=True),
            53: self._article(53, "Policies", None, "<p>Rules.</p>"),
        }
        if major >= 18:
            # Article templates are knowledge.article records from Odoo 18 on
            # (`is_template`); seen live on Odoo 19. Odoo 17 has no such field.
            for article in self.records["knowledge.article"].values():
                article["is_template"] = False
            template = self._article(54, "Meeting notes template", None, "<p>Agenda</p>")
            self.records["knowledge.article"][54] = {**template, "is_template": True}

    @staticmethod
    def _article(
        rid: int, name: str, parent: int | None, body: str, *, to_delete: bool = False
    ) -> dict[str, Any]:
        return {
            "id": rid,
            "name": name,
            "parent_id": parent,
            "body": body,
            "to_delete": to_delete,
            "write_date": WRITE_DATE,
        }

    def add_file(
        self, rid: int, name: str, folder: int, mimetype: str, data: bytes, **kw: Any
    ) -> None:
        self.records["documents.document"][rid] = _file(rid, name, folder, mimetype, data, **kw)

    def transport(self) -> httpx.MockTransport:
        return _streamed(self.handle)

    def connector(self) -> OdooKnowledgeConnector:
        return OdooKnowledgeConnector(transport=self.transport())

    def reads(self, model: str) -> list[int]:
        """The ids `read` was called for on `model`."""
        return [i for m, meth, kw in self.calls if m == model and meth == "read" for i in kw["ids"]]

    # -- protocol --

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(502, text="Bad Gateway")
        path = request.url.path
        body = json.loads(request.content or b"{}")
        if path == "/jsonrpc":
            return self._jsonrpc(body)
        if path.startswith("/json/2/"):
            if self.major < 19:
                return httpx.Response(404, text="Not Found")
            if self.json2_probe is not None:
                return httpx.Response(self.json2_probe, json={"name": "x", "message": "no"})
            _, _, _, model, method = path.split("/")
            if model == "res.users" and method == "context_get":
                return httpx.Response(200, json={"lang": "en_US"})
            try:
                return httpx.Response(200, json=self.dispatch(model, method, body))
            except _Fail as fail:
                return httpx.Response(
                    fail.status, json={"name": fail.name, "message": fail.message}
                )
        return httpx.Response(404)

    def _jsonrpc(self, body: dict[str, Any]) -> httpx.Response:
        params = body["params"]
        service, method, args = params["service"], params["method"], params["args"]
        if service == "common" and method == "version":
            return self._ok({"server_version": "x", "server_version_info": self.version})
        if service == "common" and method == "authenticate":
            return self._ok(self.authenticate)
        _db, _uid, _pw, model, meth, pos, kw = args
        kwargs = dict(kw)
        if pos:
            kwargs["ids" if meth == "read" else "domain"] = pos[0]
        try:
            return self._ok(self.dispatch(model, meth, kwargs))
        except _Fail as fail:
            name = fail.name
            message = fail.message
            if name == "werkzeug.exceptions.NotFound":
                name, message = "builtins.KeyError", repr(model)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": 200,
                        "message": "Odoo Server Error",
                        "data": {"name": name, "message": message, "debug": f"{DB} {LOGIN}"},
                    },
                },
            )

    @staticmethod
    def _ok(result: Any) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": None, "result": result})

    # -- models --

    def _installed(self, model: str) -> bool:
        return model in self.records and model not in self.missing

    def dispatch(self, model: str, method: str, kw: dict[str, Any]) -> Any:
        self.calls.append((model, method, kw))
        if model == "ir.model":
            assert method == "search_count"
            [[_field, _op, wanted]] = kw["domain"]
            return 1 if self._installed(wanted) else 0
        if model == "ir.model.fields":
            assert method == "search_count"
            terms = {f: v for f, _op, v in kw["domain"]}
            rows = self.records.get(terms["model"], {}).values()
            return 1 if any(terms["name"] in row for row in rows) else 0
        if not self._installed(model):
            raise _Fail("werkzeug.exceptions.NotFound", model, 404)
        if model in self.denied:
            raise _Fail("odoo.exceptions.AccessError", f"{LOGIN} may not read {model}", 403)
        table = self.records[model]
        if method == "read":
            out = []
            for rid in kw["ids"]:
                if rid not in table or (model, rid) in self.vanish_on_read:
                    raise _Fail("odoo.exceptions.MissingError", f"{DB} record gone", 404)
                out.append(self._shape(model, table[rid], kw.get("fields")))
            return out
        rows = [r for r in table.values() if self._matches(model, r, kw.get("domain") or [])]
        rows.sort(key=lambda r: r["id"])
        if method == "search_count":
            return len(rows)
        offset = int(kw.get("offset") or 0)
        limit = kw.get("limit")
        rows = rows[offset : offset + limit] if limit else rows[offset:]
        if method == "search":
            return [r["id"] for r in rows]
        assert method == "search_read", method
        return [self._shape(model, r, kw.get("fields")) for r in rows]

    def _shape(self, model: str, row: dict[str, Any], fields: list[str] | None) -> dict[str, Any]:
        out: dict[str, Any] = {"id": row["id"]}
        for name in fields or [k for k in row if k != "id"]:
            value = row.get(name, False)
            if name in _M2O.get(model, set()):
                target = self._comodel(model, name)
                value = [value, self.records[target][value]["name"]] if value else False
            out[name] = value
        return out

    def _comodel(self, model: str, field: str) -> str:
        if field == "id":
            return model
        if field == "folder_id":
            return "documents.folder" if self.major < 18 else "documents.document"
        if field == "parent_folder_id":
            return "documents.folder"
        return "knowledge.article"

    def _descendants(self, model: str, root: int) -> set[int]:
        table = self.records[model]
        parent = _PARENT[model]
        out: set[int] = set()
        for rid in table:
            node: int | None = rid
            while node is not None and node in table:
                if node == root:
                    out.add(rid)
                    break
                node = table[node].get(parent)
        return out

    def _matches(self, model: str, row: dict[str, Any], domain: list[Any]) -> bool:
        known = {k for r in self.records[model].values() for k in r}
        for field, op, value in domain:
            if field not in known:
                # What a live Odoo 19 answers for an unknown field in a domain.
                raise _Fail("builtins.ValueError", f"Invalid field {field!r}", 500)
            have = row.get(field)
            if op == "=":
                ok = (not have) if value is False else have == value
            elif op == "!=":
                ok = have != value
            elif op == "in":
                ok = have in value
            elif op == "child_of":
                ok = have in self._descendants(self._comodel(model, field), value)
            else:
                raise AssertionError(f"unsupported operator {op!r}")
            if not ok:
                return False
        return True


class _Auth:
    """A stand-in `AuthContext` resolving the one Odoo credential."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.values = {"url": URL, "database": DB, "username": LOGIN, "password": SECRET}

    async def token(self) -> str:
        raise AssertionError("odoo_knowledge never asks for an OAuth token")

    async def secret(self, ref: str) -> str:
        raise AssertionError("odoo_knowledge never resolves a secret ref")

    async def credential(self, credential_id: str, field_key: str) -> str:
        assert credential_id == CREDENTIAL
        if self.fail:
            raise ConnectorError("no stored credential with this id")
        return self.values[field_key]


def _config(*items: tuple[str, str]) -> dict[str, Any]:
    return {
        "credential": CREDENTIAL,
        "selection": [{"id": i, "label": i, "kind": kind} for i, kind in items],
    }


ALL_DOCUMENTS = ("app:documents", "all")
ALL_KNOWLEDGE = ("app:knowledge", "all")


async def _fetch(
    connector: OdooKnowledgeConnector,
    config: dict[str, Any],
    cursor: dict[str, Any] | None = None,
    auth: Any = None,
) -> tuple[list[RawDocument], dict[str, int], list[str]]:
    """Iterate `fetch` inside a collector, as `run_source_sync` does."""
    with collect_sync_report() as report:
        docs = [d async for d in connector.fetch(config, cursor, auth or _Auth())]
    return docs, report.skipped, report.notes


_URI_APP = {"documents.document": "documents", "knowledge.article": "knowledge"}


def _uri(model: str, rid: int) -> str:
    return f"odoo://{_URI_APP[model]}/{rid}"


def _assert_clean(text: str) -> None:
    for value in (HOST, URL, DB, LOGIN, SECRET):
        assert value not in text, f"{value!r} leaked into {text!r}"


# ---- registration and schema -------------------------------------------------


async def test_register_adds_the_connector_with_its_schema() -> None:
    added: list[Any] = []

    class _Contrib:
        def add_connector(self, connector: Any) -> None:
            added.append(connector)

    register(_Contrib())
    [connector] = added
    assert connector.type_id == "odoo_knowledge"
    assert connector.label == "Odoo (Documents & Knowledge)"
    assert connector.requires_oauth is None
    schema = connector.config_schema
    assert set(schema["required"]) == {"credential", "selection"}
    assert schema["properties"]["credential"]["credentialType"] == "odoo_login"
    selection = schema["properties"]["selection"]
    assert selection["type"] == "array"
    assert selection["x-oc8-picker"] is True
    assert selection["minItems"] == 1
    assert set(selection["items"]["required"]) == {"id", "label", "kind"}
    assert schema["properties"]["name"]["type"] == "string"


# ---- validate ----------------------------------------------------------------


async def test_validate_ok() -> None:
    result = await KnowledgeOdoo().connector().validate(_config(ALL_KNOWLEDGE), _Auth())
    assert result.ok, result.error


@pytest.mark.parametrize("selection", [None, [], "app:documents"])
async def test_validate_rejects_an_empty_or_malformed_selection(selection: Any) -> None:
    config = {"credential": CREDENTIAL, "selection": selection}
    result = await KnowledgeOdoo().connector().validate(config, _Auth())
    assert not result.ok
    assert result.error


@pytest.mark.parametrize(
    "item",
    [
        {"id": "app:sales", "label": "Everything", "kind": "all"},
        {"id": "documents.folder:abc", "label": "x", "kind": "folder"},
        {"id": "res.partner:1", "label": "x", "kind": "folder"},
        {"id": "documents.folder:1", "label": "x", "kind": "all"},
        "documents.folder:1",
    ],
)
async def test_validate_rejects_an_unknown_item(item: Any) -> None:
    config = {"credential": CREDENTIAL, "selection": [item]}
    result = await KnowledgeOdoo().connector().validate(config, _Auth())
    assert not result.ok


async def test_validate_reports_a_refused_login() -> None:
    fake = KnowledgeOdoo(17, authenticate=False)
    result = await fake.connector().validate(_config(ALL_KNOWLEDGE), _Auth())
    assert not result.ok
    assert result.error == str(OdooLoginRefused())


# ---- browse_selectable (the picker) -----------------------------------------


async def test_browse_root_shows_both_apps_and_notes() -> None:
    fake = KnowledgeOdoo(missing=("knowledge.article",), denied=("documents.document",))
    entries = await fake.connector().browse_selectable(_config(), "/", _Auth())
    by_id = {e.id: e for e in entries}
    assert set(by_id) == {"app:documents", "app:knowledge"}
    assert all(e.kind == "app" and e.is_folder for e in entries)
    assert by_id["app:knowledge"].note == "app_missing"
    assert by_id["app:documents"].note == "no_access"

    healthy = await KnowledgeOdoo().connector().browse_selectable(_config(), "/", _Auth())
    assert [e.note for e in healthy] == [None, None]


async def test_browse_documents_folders_odoo17() -> None:
    connector = KnowledgeOdoo(17).connector()
    top = await connector.browse_selectable(_config(), "app:documents", _Auth())
    assert [(e.id, e.name, e.kind, e.is_folder) for e in top] == [
        ("documents.folder:1", "Finance", "folder", True),
        ("documents.folder:3", "HR", "folder", True),
    ]
    sub = await connector.browse_selectable(_config(), "documents.folder:1", _Auth())
    assert [(e.id, e.name) for e in sub] == [("documents.folder:2", "Invoices")]


async def test_browse_documents_folders_odoo19() -> None:
    connector = KnowledgeOdoo(19).connector()
    top = await connector.browse_selectable(_config(), "app:documents", _Auth())
    assert [(e.id, e.name, e.kind, e.is_folder) for e in top] == [
        ("documents.document:1", "Finance", "folder", True),
        ("documents.document:3", "HR", "folder", True),
    ]
    sub = await connector.browse_selectable(_config(), "documents.document:1", _Auth())
    # Only folders: the file report.pdf in Finance is not offered.
    assert [(e.id, e.name) for e in sub] == [("documents.document:2", "Invoices")]


async def test_browse_knowledge_tree() -> None:
    connector = KnowledgeOdoo().connector()
    roots = await connector.browse_selectable(_config(), "app:knowledge", _Auth())
    assert [(e.id, e.name, e.kind) for e in roots] == [
        ("knowledge.article:50", "Handbook", "article"),
        ("knowledge.article:53", "Policies", "article"),
    ]
    children = await connector.browse_selectable(_config(), "knowledge.article:50", _Auth())
    assert [(e.id, e.name) for e in children] == [("knowledge.article:51", "Onboarding")]


@pytest.mark.parametrize("path", ["res.partner:1", "app:sales", "documents.folder:x", "../etc"])
async def test_browse_refuses_an_unknown_path(path: str) -> None:
    with pytest.raises(ConnectorError):
        await KnowledgeOdoo().connector().browse_selectable(_config(), path, _Auth())


# ---- fetch -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("major", "folder"), [(17, "documents.folder:1"), (19, "documents.document:1")]
)
async def test_fetch_documents_and_articles(major: int, folder: str) -> None:
    fake = KnowledgeOdoo(major)
    config = _config((folder, "folder"), ("knowledge.article:50", "article"))
    docs, skipped, notes = await _fetch(fake.connector(), config)
    by_uri = {d.source_uri: d for d in docs}
    assert set(by_uri) == {
        _uri("documents.document", 10),
        _uri("documents.document", 11),
        _uri("knowledge.article", 50),
        _uri("knowledge.article", 51),
    }
    assert skipped == {}
    assert notes == []
    # The database name stays out of every URI: they reach prompts and audit.
    assert "odoo://documents/10" in by_uri
    assert "odoo://knowledge/50" in by_uri
    for uri in by_uri:
        _assert_clean(uri)

    pdf = by_uri[_uri("documents.document", 10)]
    assert pdf.title == "report.pdf"
    assert pdf.content_type == "application/pdf"
    assert base64.b64decode(pdf.content) == PDF
    assert pdf.metadata["app"] == "documents"
    assert pdf.metadata["path"] == "Finance"
    assert pdf.metadata["write_date"] == WRITE_DATE
    expected_url = (
        f"{URL}/odoo/documents/10"
        if major >= 18
        else f"{URL}/web#model=documents.document&id=10&view_type=form"
    )
    assert pdf.metadata["url"] == expected_url

    txt = by_uri[_uri("documents.document", 11)]
    assert txt.content_type == "text/plain"
    assert txt.content == TXT.decode()
    assert txt.metadata["path"] == "Invoices"

    child = by_uri[_uri("knowledge.article", 51)]
    assert child.content_type == "text/html"
    assert child.content == "<p>Day one.</p>"
    assert child.title == "Onboarding"
    assert child.metadata["app"] == "knowledge"
    assert child.metadata["path"] == "Handbook"


async def test_fetch_dedupes_overlapping_selection() -> None:
    fake = KnowledgeOdoo()
    config = _config(
        ALL_DOCUMENTS,
        ("documents.document:1", "folder"),
        ("documents.document:2", "folder"),
        ("knowledge.article:50", "article"),
        ("knowledge.article:51", "article"),
    )
    docs, _, _ = await _fetch(fake.connector(), config)
    uris = [d.source_uri for d in docs]
    assert len(uris) == len(set(uris))
    assert set(uris) == {
        _uri("documents.document", 10),
        _uri("documents.document", 11),
        _uri("documents.document", 12),
        _uri("knowledge.article", 50),
        _uri("knowledge.article", 51),
    }
    # Each file's content is read once.
    assert sorted(fake.reads("documents.document")) == [10, 11, 12]


async def test_fetch_skips_and_counts() -> None:
    fake = KnowledgeOdoo()
    fake.add_file(20, "photo.png", 3, "image/png", b"\x89PNG")
    fake.add_file(21, "big.pdf", 3, "application/pdf", b"%PDF", file_size=MAX_BYTES + 1)
    fake.add_file(22, "Odoo website", 3, False, b"", type="url")  # type: ignore[arg-type]
    docs, skipped, _ = await _fetch(fake.connector(), _config(("documents.document:3", "folder")))
    assert [d.title for d in docs] == ["notes.md"]
    assert skipped == {"unsupported_type": 1, "too_large": 1, "url_document": 1}
    # A skipped file is never downloaded.
    assert fake.reads("documents.document") == [12]


async def test_skips_of_concurrent_syncs_stay_apart() -> None:
    """One connector instance serves every sync; each sync's collector sees
    only its own skips."""
    fake = KnowledgeOdoo()
    fake.add_file(20, "photo.png", 3, "image/png", b"\x89PNG")
    connector = fake.connector()

    async def run(config: dict[str, Any]) -> dict[str, int]:
        _, skipped, _ = await _fetch(connector, config)
        return skipped

    with_skip, without = await asyncio.gather(
        run(_config(("documents.document:3", "folder"))),
        run(_config(("documents.document:1", "folder"))),
    )
    assert with_skip == {"unsupported_type": 1}
    assert without == {}


async def test_extractable_types_are_fetched() -> None:
    fake = KnowledgeOdoo()
    types = {
        30: "text/html",
        31: "text/csv",
        32: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        33: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        34: "text/plain; charset=utf-8",
    }
    for rid, mimetype in types.items():
        fake.add_file(rid, f"f{rid}", 3, mimetype, b"a,b\n1,2")
    docs, skipped, _ = await _fetch(fake.connector(), _config(("documents.document:3", "folder")))
    got = {d.title: d.content_type for d in docs}
    assert got["f30"] == "text/html"
    assert got["f31"] == "text/csv"
    assert got["f34"] == "text/plain"
    assert base64.b64decode(next(d for d in docs if d.title == "f32").content) == b"a,b\n1,2"
    assert skipped == {}


async def test_oversized_article_body_skipped() -> None:
    fake = KnowledgeOdoo()
    fake.records["knowledge.article"][53]["body"] = "<p>" + "x" * MAX_BYTES + "</p>"
    docs, skipped, _ = await _fetch(fake.connector(), _config(ALL_KNOWLEDGE))
    assert {d.title for d in docs} == {"Handbook", "Onboarding"}
    assert skipped == {"too_large": 1}


async def test_article_body_beyond_the_answer_cap_is_skipped_too() -> None:
    """A body so large the client refuses the answer is a skip, not a
    failed sync."""
    fake = KnowledgeOdoo()
    fake.records["knowledge.article"][53]["body"] = "x" * (17 * 1024 * 1024)
    docs, skipped, _ = await _fetch(fake.connector(), _config(ALL_KNOWLEDGE))
    assert {d.title for d in docs} == {"Handbook", "Onboarding"}
    assert skipped == {"too_large": 1}


async def test_content_hash_stable_on_same_write_date() -> None:
    fake = KnowledgeOdoo()
    config = _config(("documents.document:1", "folder"), ("knowledge.article:50", "article"))
    first, _, _ = await _fetch(fake.connector(), config)
    second, _, _ = await _fetch(fake.connector(), config)
    assert {d.source_uri: d.content_hash for d in first} == {
        d.source_uri: d.content_hash for d in second
    }
    pdf = next(d for d in first if d.title == "report.pdf")
    expected = hashlib.sha256(f"documents.document:10:{WRITE_DATE}".encode()).hexdigest()
    assert pdf.content_hash == expected

    fake.records["documents.document"][10]["write_date"] = "2026-11-02 08:00:00"
    third, _, _ = await _fetch(fake.connector(), config)
    changed = next(d for d in third if d.title == "report.pdf")
    assert changed.content_hash != pdf.content_hash


async def test_unchanged_records_are_not_downloaded_again() -> None:
    fake = KnowledgeOdoo()
    config = _config(("documents.document:1", "folder"), ("knowledge.article:50", "article"))
    first, _, _ = await _fetch(fake.connector(), config)
    fake.calls.clear()
    cursor = {"hashes": [d.content_hash for d in first]}
    again, skipped, _ = await _fetch(fake.connector(), config, cursor)
    assert again == []
    assert skipped == {}
    assert fake.reads("documents.document") == []
    assert fake.reads("knowledge.article") == []


async def test_deleted_ticked_folder_contributes_nothing() -> None:
    fake = KnowledgeOdoo()
    config = _config(
        ("documents.document:99", "folder"),
        ("knowledge.article:98", "article"),
        ("knowledge.article:53", "article"),
    )
    docs, skipped, _ = await _fetch(fake.connector(), config)
    assert [d.title for d in docs] == ["Policies"]
    assert skipped == {}
    listing = await fake.connector().attest_listing(config, _Auth())
    assert listing.attestation == Attestation.AUTHORITATIVE
    assert listing.present_uris == frozenset({_uri("knowledge.article", 53)})


async def test_a_record_gone_mid_sync_is_skipped_silently() -> None:
    fake = KnowledgeOdoo()
    fake.vanish_on_read = {("documents.document", 10), ("knowledge.article", 51)}
    config = _config(("documents.document:1", "folder"), ("knowledge.article:50", "article"))
    docs, skipped, _ = await _fetch(fake.connector(), config)
    assert {d.title for d in docs} == {"q1.txt", "Handbook"}
    assert skipped == {}


async def test_trashed_articles_are_left_out() -> None:
    docs, _, _ = await _fetch(KnowledgeOdoo().connector(), _config(ALL_KNOWLEDGE))
    assert "Old" not in {d.title for d in docs}


@pytest.mark.parametrize("label", ["Everything", "Alles", "whatever"])
async def test_selection_matches_kind_not_label(label: str) -> None:
    config = {
        "credential": CREDENTIAL,
        "selection": [{"id": "app:knowledge", "label": label, "kind": "all"}],
    }
    docs, _, _ = await _fetch(KnowledgeOdoo().connector(), config)
    assert {d.title for d in docs} == {"Handbook", "Onboarding", "Policies"}


async def test_a_folder_id_from_the_other_odoo_generation_contributes_nothing() -> None:
    """A 17-style folder id on Odoo 19 (and the reverse) matches nothing."""
    docs19, _, _ = await _fetch(
        KnowledgeOdoo(19).connector(), _config(("documents.folder:1", "folder"))
    )
    docs17, _, _ = await _fetch(
        KnowledgeOdoo(17).connector(), _config(("documents.document:1", "folder"))
    )
    assert docs19 == [] and docs17 == []


# ---- attest_listing ----------------------------------------------------------


async def test_attest_listing_holds_every_uri_of_the_resolved_selection() -> None:
    fake = KnowledgeOdoo()
    fake.add_file(20, "photo.png", 3, "image/png", b"\x89PNG")
    config = _config(ALL_DOCUMENTS, ("knowledge.article:50", "article"))
    listing = await fake.connector().attest_listing(config, _Auth())
    assert listing.attestation == Attestation.AUTHORITATIVE
    # Folders are not documents; a skipped file is still there.
    assert listing.present_uris == frozenset(
        {
            _uri("documents.document", 10),
            _uri("documents.document", 11),
            _uri("documents.document", 12),
            _uri("documents.document", 20),
            _uri("knowledge.article", 50),
            _uri("knowledge.article", 51),
        }
    )


async def test_attest_listing_re_resolves_from_config() -> None:
    """No state from an earlier fetch: the listing follows the config it is
    given, and a fetch of another config in between changes nothing."""
    fake = KnowledgeOdoo()
    connector = fake.connector()
    await _fetch(connector, _config(ALL_DOCUMENTS))
    listing = await connector.attest_listing(_config(("knowledge.article:53", "article")), _Auth())
    assert listing.present_uris == frozenset({_uri("knowledge.article", 53)})


@pytest.mark.parametrize(
    ("fake", "error"),
    [
        (KnowledgeOdoo(denied=("documents.document",)), OdooAccessDenied),
        (KnowledgeOdoo(17, denied=("documents.document",)), OdooAccessDenied),
        (KnowledgeOdoo(missing=("documents.document",)), OdooAppMissing),
        (KnowledgeOdoo(17, missing=("documents.document",)), OdooAppMissing),
    ],
)
async def test_whole_app_unreadable_raises_in_fetch_and_attest(
    fake: KnowledgeOdoo, error: type[Exception]
) -> None:
    config = _config(ALL_DOCUMENTS, ALL_KNOWLEDGE)
    with pytest.raises(error):
        await _fetch(fake.connector(), config)
    with pytest.raises(error):
        await fake.connector().attest_listing(config, _Auth())


async def test_missing_credential_raises_in_fetch_and_attest() -> None:
    fake = KnowledgeOdoo()
    config = _config(ALL_KNOWLEDGE)
    with pytest.raises(ConnectorError):
        await _fetch(fake.connector(), config, auth=_Auth(fail=True))
    with pytest.raises(ConnectorError):
        await fake.connector().attest_listing(config, _Auth(fail=True))
    with pytest.raises(ConnectorError):
        await fake.connector().attest_listing(config, None)
    assert fake.calls == []


async def test_attest_listing_raises_for_an_empty_selection() -> None:
    with pytest.raises(ConnectorError):
        await (
            KnowledgeOdoo()
            .connector()
            .attest_listing({"credential": CREDENTIAL, "selection": []}, _Auth())
        )


async def test_unreachable_odoo_raises_everywhere() -> None:
    fake = KnowledgeOdoo(down=True)
    config = _config(ALL_KNOWLEDGE)
    with pytest.raises(OdooUnreachable):
        await _fetch(fake.connector(), config)
    with pytest.raises(OdooUnreachable):
        await fake.connector().attest_listing(config, _Auth())


# ---- browse (chat pinning), in_scope, fetch_file -----------------------------


async def test_chat_browse_lists_only_inside_the_selection() -> None:
    connector = KnowledgeOdoo().connector()
    config = _config(("documents.document:1", "folder"), ("knowledge.article:50", "article"))
    root = await connector.browse(config, "/", _Auth())
    assert [(e.id, e.name, e.is_folder) for e in root] == [
        ("documents.document:1", "Finance", True),
        ("knowledge.article:50", "Handbook", True),
    ]
    finance = await connector.browse(config, "documents.document:1", _Auth())
    assert [(e.id, e.name, e.is_folder) for e in finance] == [
        ("documents.document:2", "Invoices", True),
        ("documents.document:10", "report.pdf", False),
    ]
    handbook = await connector.browse(config, "knowledge.article:50", _Auth())
    assert [(e.id, e.name, e.is_folder) for e in handbook] == [
        ("knowledge.article:50", "Handbook", False),
        ("knowledge.article:51", "Onboarding", True),
    ]
    for outside in ("documents.document:3", "knowledge.article:53", "app:documents"):
        with pytest.raises(ConnectorError):
            await connector.browse(config, outside, _Auth())


async def test_chat_browse_of_a_whole_app() -> None:
    connector = KnowledgeOdoo(17).connector()
    config = _config(ALL_DOCUMENTS)
    root = await connector.browse(config, "/", _Auth())
    assert [(e.id, e.kind) for e in root] == [("app:documents", "app")]
    top = await connector.browse(config, "app:documents", _Auth())
    assert [e.id for e in top] == ["documents.folder:1", "documents.folder:3"]
    hr = await connector.browse(config, "documents.folder:3", _Auth())
    assert [(e.id, e.is_folder) for e in hr] == [("documents.document:12", False)]


async def test_in_scope_refuses_outside_selection() -> None:
    connector = KnowledgeOdoo().connector()
    config = _config(("documents.document:1", "folder"), ("knowledge.article:50", "article"))
    assert await connector.in_scope(config, "documents.document:10", _Auth()) is True
    assert await connector.in_scope(config, "documents.document:11", _Auth()) is True
    assert await connector.in_scope(config, "knowledge.article:51", _Auth()) is True
    for outside in (
        "documents.document:12",  # in HR, not ticked
        "documents.document:2",  # a folder, not a file
        "knowledge.article:53",
        "knowledge.article:99",
        "res.partner:1",
        "documents.document:../1",
        "",
    ):
        assert await connector.in_scope(config, outside, _Auth()) is False, outside


async def test_fetch_file_respects_max_bytes() -> None:
    fake = KnowledgeOdoo()
    connector = fake.connector()
    config = _config(("documents.document:1", "folder"), ALL_KNOWLEDGE)
    got = await connector.fetch_file(config, "documents.document:11", 1024, _Auth())
    assert got.name == "q1.txt"
    assert got.content_type == "text/plain"
    assert got.data == TXT
    assert got.text == TXT.decode()

    article = await connector.fetch_file(config, "knowledge.article:51", 1024, _Auth())
    assert article.content_type == "text/html"
    assert article.text == "Day one."

    with pytest.raises(ConnectorError):
        await connector.fetch_file(config, "documents.document:11", len(TXT) - 1, _Auth())
    with pytest.raises(ConnectorError):
        await connector.fetch_file(config, "knowledge.article:51", 4, _Auth())
    fake.calls.clear()
    with pytest.raises(ConnectorError):
        await connector.fetch_file(config, "documents.document:12", 1024, _Auth())
    assert fake.reads("documents.document") == []


# ---- notes and secrets -------------------------------------------------------


async def test_notes_report_api_key_hint() -> None:
    fake = KnowledgeOdoo(19, json2_probe=401)
    connector = fake.connector()
    docs, _, notes = await _fetch(connector, _config(ALL_KNOWLEDGE))
    assert notes == ["api_key_recommended"]
    assert docs
    result = await connector.validate(_config(ALL_KNOWLEDGE), _Auth())
    assert result.ok

    _, _, clean = await _fetch(KnowledgeOdoo(19).connector(), _config(ALL_KNOWLEDGE))
    assert clean == []


async def test_no_secret_in_any_error(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    scenarios = [
        KnowledgeOdoo(17, authenticate=False),
        KnowledgeOdoo(19, denied=("documents.document", "knowledge.article")),
        KnowledgeOdoo(17, missing=("documents.document", "knowledge.article")),
        KnowledgeOdoo(down=True),
    ]
    config = _config(ALL_DOCUMENTS, ALL_KNOWLEDGE)
    messages: list[str] = []
    for fake in scenarios:
        connector = fake.connector()
        result = await connector.validate(config, _Auth())
        messages.append(result.error or "")
        calls = [
            _fetch(connector, config),
            connector.attest_listing(config, _Auth()),
            connector.browse_selectable(config, "app:documents", _Auth()),
            connector.browse(config, "app:documents", _Auth()),
            connector.fetch_file(config, "documents.document:10", 1024, _Auth()),
        ]
        for call in calls:
            try:
                await call
            except ConnectorError as exc:
                messages.append(str(exc))
                cause: BaseException | None = exc
                while cause is not None:
                    messages.append(repr(cause))
                    cause = cause.__cause__ or cause.__context__
    malformed = await KnowledgeOdoo().connector().validate(config | {"credential": ""}, _Auth())
    messages.append(malformed.error or "")
    assert any(messages)
    for message in messages:
        _assert_clean(message)
    for record in caplog.records:
        _assert_clean(record.getMessage())


@pytest.mark.parametrize("major", [17, 19])
async def test_article_templates_are_not_knowledge(major: int) -> None:
    """Odoo 18+ keeps article templates as knowledge.article records; they are
    not content. Odoo 17 has no `is_template` field, and the connector must
    not put one in a domain there."""
    fake = KnowledgeOdoo(major)
    connector = fake.connector()
    docs, _, _ = await _fetch(connector, _config(ALL_KNOWLEDGE))
    assert {d.title for d in docs} == {"Handbook", "Onboarding", "Policies"}
    listing = await connector.attest_listing(_config(ALL_KNOWLEDGE), _Auth())
    assert _uri("knowledge.article", 54) not in listing.present_uris
    roots = await connector.browse_selectable(_config(), "app:knowledge", _Auth())
    assert [e.name for e in roots] == ["Handbook", "Policies"]
    chat = await connector.browse(_config(ALL_KNOWLEDGE), "app:knowledge", _Auth())
    assert [e.name for e in chat] == ["Handbook", "Policies"]
    if major >= 18:
        assert (
            await connector.in_scope(_config(ALL_KNOWLEDGE), "knowledge.article:54", _Auth())
            is False
        )


@pytest.mark.parametrize("major", [17, 19])
async def test_shortcut_documents_are_not_ingested(major: int) -> None:
    """A shortcut (Odoo 18+) in a ticked folder points at a file that may live
    in an unticked one; only the file itself is content. Odoo 17 has no
    `shortcut_document_id`, and the connector must not put one in a domain."""
    fake = KnowledgeOdoo(major)
    if major >= 18:
        # A shortcut in HR (3) to q1.txt (11), which lives in Invoices (2).
        fake.add_file(30, "q1.txt", 3, "text/plain", TXT, shortcut_document_id=11)
    connector = fake.connector()
    config = _config(("documents.document:3" if major >= 18 else "documents.folder:3", "folder"))
    docs, _, _ = await _fetch(connector, config)
    assert [d.source_uri for d in docs] == [_uri("documents.document", 12)]
    listing = await connector.attest_listing(config, _Auth())
    assert listing.present_uris == frozenset({_uri("documents.document", 12)})
    if major >= 18:
        assert await connector.in_scope(config, "documents.document:30", _Auth()) is False
