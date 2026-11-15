"""Odoo Documents and Knowledge as a knowledge source (`odoo_knowledge`).

The source config holds the id of an `odoo_login` credential and a
`selection` picked in the source wizard (`x-oc8-picker`). Each selection item
is `{id, label, kind}`:

* `kind == "all"` with id `app:documents` or `app:knowledge`: everything in
  that app. Only `kind` decides this, never the label ("Everything", "Alles").
* `documents.folder:<id>`: an Odoo 17 Documents folder.
* `documents.document:<id>`: an Odoo 18+ Documents folder (folders are
  `documents.document` records with `type = 'folder'` there).
* `knowledge.article:<id>`: an article and all its descendants.

A folder covers its subfolders and an article its descendants (`child_of`).
Every search runs as the stored login, so Odoo's access rights and record
rules decide what is seen.

Nothing about a sync is kept on the connector: one instance serves every
tenant and every concurrent sync. Each method resolves the selection from the
config it is given. Skips and notes go to the sync's own collector
(`oc8.knowledge.connectors.report`).

What a sync takes:

* Documents files whose mimetype core can extract text from, up to 5 MiB.
  URL documents, other types and larger files are skipped and counted.
* Knowledge articles (not in the trash, not templates) as `text/html`, bodies
  up to 5 MiB.
* `content_hash` is built from the model, the id and `write_date`, so an
  unchanged record is not downloaded again.

Errors are the Odoo client's fixed phrases or fixed phrases from here. The
url, database, login and secret never appear in one.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx

from oc8.knowledge.connectors.base import (
    Attestation,
    AuthContext,
    BrowseEntry,
    ConnectorError,
    FetchedFile,
    RawDocument,
    SourceItemMeta,
    SourceListing,
    ValidationResult,
)
from oc8.knowledge.connectors.report import report_note, report_skip

from .odoo_client import (
    HINT_API_KEY_RECOMMENDED,
    OdooAppMissing,
    OdooClient,
    OdooError,
    OdooRecordMissing,
    open_client,
)

logger = logging.getLogger(__name__)

#: The most of one file or article body a sync takes, as for other connectors.
MAX_BYTES = 5 * 1024 * 1024

#: What core's text extraction understands.
EXTRACTABLE_TYPES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/html",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/csv",
    }
)
#: Passed to core as text; every other extractable type travels base64.
_TEXT_TYPES = frozenset({"text/plain", "text/markdown", "text/html", "text/csv"})

APP_DOCUMENTS = "app:documents"
APP_KNOWLEDGE = "app:knowledge"
ALL_KIND = "all"

_FOLDER_17 = "documents.folder"
_DOCUMENT = "documents.document"
_ARTICLE = "knowledge.article"

_ITEM_ID = re.compile(
    r"^(documents\.folder|documents\.document|knowledge\.article):([1-9][0-9]{0,17})$"
)

#: Records per search page.
_PAGE = 1000
#: One more than core shows, so core can tell the listing was cut.
_BROWSE_LIMIT = 501
#: How many items `discover` previews.
_DISCOVER_LIMIT = 100

_DOC_FIELDS = ["name", "mimetype", "file_size", "type", "folder_id", "write_date"]
_ARTICLE_FIELDS = ["name", "parent_id", "write_date"]

#: The client's phrase for an answer over its size cap. The client raises a
#: plain OdooError for it, and a single record that large is a skip.
_ANSWER_TOO_LARGE = "The Odoo answer is too large."

_NOT_IN_SCOPE = "This Odoo record is not part of the source."

# Skip reasons and note codes, shown on the sync result.
SKIP_UNSUPPORTED = "unsupported_type"
SKIP_TOO_LARGE = "too_large"
SKIP_URL = "url_document"
SKIP_UNREADABLE = "unreadable"
NOTE_APP_MISSING = "app_missing"
NOTE_NO_ACCESS = "no_access"


# ---- the selection -----------------------------------------------------------


@dataclass(frozen=True)
class _Selection:
    documents_all: bool
    knowledge_all: bool
    folders_17: frozenset[int]
    folders: frozenset[int]
    articles: frozenset[int]

    @property
    def uses_documents(self) -> bool:
        return self.documents_all or bool(self.folders_17) or bool(self.folders)

    @property
    def uses_knowledge(self) -> bool:
        return self.knowledge_all or bool(self.articles)


def _parse_item(value: Any) -> tuple[str, int]:
    """`(model, id)` from an item id like `knowledge.article:12`."""
    match = _ITEM_ID.match(value) if isinstance(value, str) else None
    if match is None:
        raise ConnectorError("This is not a valid Odoo folder or article.")
    return match.group(1), int(match.group(2))


def _parse_selection(config: dict[str, Any]) -> _Selection:
    raw = config.get("selection")
    if not isinstance(raw, list) or not raw:
        raise ConnectorError("Choose at least one Odoo folder or article.")
    documents_all = knowledge_all = False
    picked: dict[str, set[int]] = {_FOLDER_17: set(), _DOCUMENT: set(), _ARTICLE: set()}
    for item in raw:
        if not isinstance(item, dict):
            raise ConnectorError("The Odoo selection is not valid.")
        item_id = item.get("id")
        if item.get("kind") == ALL_KIND:
            if item_id == APP_DOCUMENTS:
                documents_all = True
            elif item_id == APP_KNOWLEDGE:
                knowledge_all = True
            else:
                raise ConnectorError("The Odoo selection is not valid.")
            continue
        try:
            model, rid = _parse_item(item_id)
        except ConnectorError:
            raise ConnectorError("The Odoo selection is not valid.") from None
        picked[model].add(rid)
    return _Selection(
        documents_all=documents_all,
        knowledge_all=knowledge_all,
        folders_17=frozenset(picked[_FOLDER_17]),
        folders=frozenset(picked[_DOCUMENT]),
        articles=frozenset(picked[_ARTICLE]),
    )


Domain = list[tuple[str, str, Any]]


def _file_domains(selection: _Selection, major: int) -> list[Domain]:
    """One `documents.document` domain per selected item, for the files it
    covers. A folder id of the other Odoo generation matches nothing."""
    not_folder: Domain = [("type", "!=", "folder")]
    if selection.documents_all:
        return [not_folder]
    folders = selection.folders_17 if major < 18 else selection.folders
    return [[("folder_id", "child_of", f), *not_folder] for f in sorted(folders)]


def _folder_scope(selection: _Selection, major: int) -> tuple[str, list[Domain]]:
    """The folder model and one domain per selected item, for the folders it
    covers (itself and below)."""
    if major < 18:
        model, base, folders = _FOLDER_17, [], selection.folders_17
    else:
        model, base, folders = _DOCUMENT, [("type", "=", "folder")], selection.folders
    if selection.documents_all:
        return model, [list(base)]
    return model, [[("id", "child_of", f), *base] for f in sorted(folders)]


def _article_domains(selection: _Selection, live: Domain) -> list[Domain]:
    """One domain per selected item; `live` is `_live_articles`."""
    if selection.knowledge_all:
        return [live]
    return [[("id", "child_of", a), *live] for a in sorted(selection.articles)]


# ---- small helpers -----------------------------------------------------------


#: The app each ingested model's `source_uri` names.
_URI_APP = {_DOCUMENT: "documents", _ARTICLE: "knowledge"}


def _uri(model: str, record_id: int) -> str:
    """`odoo://documents/<id>` or `odoo://knowledge/<id>`. The database name
    stays out: a `source_uri` reaches the model's prompt, audit events and API
    answers."""
    return f"odoo://{_URI_APP[model]}/{record_id}"


def _content_hash(model: str, record_id: int, write_date: Any) -> str:
    return hashlib.sha256(f"{model}:{record_id}:{write_date}".encode()).hexdigest()


def _record_id(record: Any) -> int | None:
    rid = record.get("id") if isinstance(record, dict) else None
    return rid if isinstance(rid, int) and not isinstance(rid, bool) and rid > 0 else None


def _m2o_name(value: Any) -> str:
    """The display name of a many2one value (`[id, name]`), or ""."""
    if isinstance(value, list) and len(value) == 2 and isinstance(value[1], str):
        return value[1]
    return ""


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _mimetype(value: Any) -> str:
    return _text(value).split(";", 1)[0].strip().lower()


def _as_list(answer: Any) -> list[dict[str, Any]]:
    if not isinstance(answer, list):
        raise OdooError("Odoo answered in an unexpected shape.")
    return [row for row in answer if isinstance(row, dict)]


def _is_answer_too_large(exc: OdooError) -> bool:
    return type(exc) is OdooError and str(exc) == _ANSWER_TOO_LARGE


async def _search_all(
    client: OdooClient, model: str, domain: Domain, fields: list[str]
) -> list[dict[str, Any]]:
    """Every record matching `domain`, page by page."""
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = _as_list(
            await client.call(
                model,
                "search_read",
                domain=domain,
                fields=fields,
                limit=_PAGE,
                offset=offset,
                order="id",
            )
        )
        out.extend(page)
        if len(page) < _PAGE:
            return out
        offset += _PAGE


async def _count(client: OdooClient, model: str, domain: Domain) -> int:
    count = await client.call(model, "search_count", domain=domain)
    return count if isinstance(count, int) and not isinstance(count, bool) else 0


async def _any_match(client: OdooClient, model: str, record_id: int, domains: list[Domain]) -> bool:
    for domain in domains:
        if await _count(client, model, [("id", "=", record_id), *domain]) > 0:
            return True
    return False


async def _live_articles(client: OdooClient) -> Domain:
    """Articles that are content: not in the trash, and not templates. From
    Odoo 18 on, templates are knowledge.article records (`is_template`);
    Odoo 17 has no such field, and an unknown field in a domain fails."""
    live: Domain = [("to_delete", "=", False)]
    has_templates = await _count(
        client, "ir.model.fields", [("model", "=", _ARTICLE), ("name", "=", "is_template")]
    )
    if has_templates:
        live.append(("is_template", "=", False))
    return live


async def _require_app(client: OdooClient, model: str) -> None:
    if not await client.model_exists(model):
        raise OdooAppMissing()


async def _read_one(client: OdooClient, model: str, record_id: int, fields: list[str]) -> Any:
    """One record's fields, or None when it is gone or hidden."""
    rows = _as_list(await client.call(model, "read", ids=[record_id], fields=fields))
    return rows[0] if rows else None


class _Undecodable(OdooError):
    default_message = "Odoo sent a file that cannot be decoded."


def _decode_datas(value: Any) -> bytes | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise _Undecodable() from None


def _entries(
    rows: list[dict[str, Any]], model: str, *, kind: str, is_folder: bool
) -> list[BrowseEntry]:
    out = []
    for row in rows:
        rid = _record_id(row)
        if rid is not None:
            out.append(
                BrowseEntry(
                    id=f"{model}:{rid}", name=_text(row.get("name")), is_folder=is_folder, kind=kind
                )
            )
    return out


@dataclass(frozen=True)
class _Conn:
    client: OdooClient


# ---- the connector -----------------------------------------------------------


class OdooKnowledgeConnector:
    type_id = "odoo_knowledge"
    label = "Odoo (Documents & Knowledge)"
    description = "Files from Odoo Documents and articles from Odoo Knowledge"
    requires_oauth: str | None = None
    config_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "credential": {
                "type": "string",
                "title": "Odoo login",
                "credentialType": "odoo_login",
            },
            "selection": {
                "type": "array",
                "title": "Folders and articles",
                "description": "What this source reads from Odoo. A folder includes its "
                "subfolders and an article its subarticles.",
                "x-oc8-picker": True,
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "label": {"type": "string"},
                        "kind": {"type": "string"},
                    },
                    "required": ["id", "label", "kind"],
                },
            },
            "name": {"type": "string", "title": "Name"},
        },
        "required": ["credential", "selection"],
    }

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # For tests only: replaces the network beneath the guarded transport.
        self._transport = transport

    # -- connection --

    @asynccontextmanager
    async def _session(
        self, config: dict[str, Any], auth: AuthContext | None
    ) -> AsyncIterator[_Conn]:
        if auth is None:
            raise ConnectorError("No credential is available for this source.")
        credential_id = str(config.get("credential") or "").strip()
        if not credential_id:
            raise ConnectorError("Choose an Odoo login.")
        values: dict[str, str] = {}
        for key in ("url", "database", "username", "password"):
            try:
                values[key] = str(await auth.credential(credential_id, key) or "")
            except ConnectorError:
                raise
            except Exception as exc:
                logger.info("odoo login could not be read: %s", type(exc).__name__)
                raise ConnectorError("The Odoo login could not be read.") from None
        client = await open_client(
            url=values["url"],
            database=values["database"],
            login=values["username"],
            secret=values["password"],
            transport=self._transport,
        )
        try:
            yield _Conn(client=client)
        finally:
            await client.aclose()

    # -- resolution --

    async def _resolve(
        self, client: OdooClient, selection: _Selection
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """The files and articles the selection covers, each once, by id.
        An app that is not installed, or one the login may not read, raises."""
        files: dict[int, dict[str, Any]] = {}
        articles: dict[int, dict[str, Any]] = {}
        if selection.uses_documents:
            await _require_app(client, _DOCUMENT)
            for domain in _file_domains(selection, client.version[0]):
                for row in await _search_all(client, _DOCUMENT, domain, _DOC_FIELDS):
                    rid = _record_id(row)
                    if rid is not None and row.get("type") != "folder":
                        files.setdefault(rid, row)
        if selection.uses_knowledge:
            await _require_app(client, _ARTICLE)
            live = await _live_articles(client)
            for domain in _article_domains(selection, live):
                for row in await _search_all(client, _ARTICLE, domain, _ARTICLE_FIELDS):
                    rid = _record_id(row)
                    if rid is not None:
                        articles.setdefault(rid, row)
        return [files[k] for k in sorted(files)], [articles[k] for k in sorted(articles)]

    # -- Connector --

    async def validate(
        self, config: dict[str, Any], auth: AuthContext | None = None
    ) -> ValidationResult:
        try:
            _parse_selection(config)
            async with self._session(config, auth):
                pass
        except ConnectorError as exc:
            return ValidationResult(ok=False, error=str(exc))
        return ValidationResult(ok=True)

    async def discover(
        self, config: dict[str, Any], auth: AuthContext | None = None
    ) -> list[SourceItemMeta]:
        selection = _parse_selection(config)
        async with self._session(config, auth) as conn:
            files, articles = await self._resolve(conn.client, selection)
        items = [
            SourceItemMeta(uri=_uri(_DOCUMENT, row["id"]), title=_text(row.get("name")))
            for row in files
        ] + [
            SourceItemMeta(uri=_uri(_ARTICLE, row["id"]), title=_text(row.get("name")))
            for row in articles
        ]
        return items[:_DISCOVER_LIMIT]

    async def attest_listing(
        self, config: dict[str, Any], auth: AuthContext | None = None
    ) -> SourceListing:
        """Every file and article the selection covers right now, resolved
        from `config` alone. Any failure raises; it never attests an empty
        set for an Odoo that did not answer."""
        selection = _parse_selection(config)
        async with self._session(config, auth) as conn:
            files, articles = await self._resolve(conn.client, selection)
        uris = {_uri(_DOCUMENT, row["id"]) for row in files}
        uris |= {_uri(_ARTICLE, row["id"]) for row in articles}
        return SourceListing(attestation=Attestation.AUTHORITATIVE, present_uris=frozenset(uris))

    async def fetch(
        self,
        config: dict[str, Any],
        cursor: dict[str, Any] | None,
        auth: AuthContext | None = None,
    ) -> AsyncIterator[RawDocument]:
        selection = _parse_selection(config)
        known = set((cursor or {}).get("hashes") or [])
        async with self._session(config, auth) as conn:
            if conn.client.hint == HINT_API_KEY_RECOMMENDED:
                report_note(HINT_API_KEY_RECOMMENDED)
            files, articles = await self._resolve(conn.client, selection)
            seen: set[str] = set()
            for row in files:
                doc = await self._file_document(conn, row, known)
                if doc is not None and doc.source_uri not in seen:
                    seen.add(doc.source_uri)
                    yield doc
            for row in articles:
                doc = await self._article_document(conn, row, known)
                if doc is not None and doc.source_uri not in seen:
                    seen.add(doc.source_uri)
                    yield doc

    async def _file_document(
        self, conn: _Conn, row: dict[str, Any], known: set[str]
    ) -> RawDocument | None:
        rid: int = row["id"]
        kind = row.get("type")
        if kind == "url":
            report_skip(SKIP_URL)
            return None
        if kind == "empty":
            return None  # a requested document with no file yet
        mimetype = _mimetype(row.get("mimetype"))
        if mimetype not in EXTRACTABLE_TYPES:
            report_skip(SKIP_UNSUPPORTED)
            return None
        size = row.get("file_size")
        if isinstance(size, int) and size > MAX_BYTES:
            report_skip(SKIP_TOO_LARGE)
            return None
        write_date = row.get("write_date")
        digest = _content_hash(_DOCUMENT, rid, write_date)
        if digest in known:
            return None  # unchanged since it was ingested
        try:
            record = await _read_one(conn.client, _DOCUMENT, rid, ["datas"])
            data = _decode_datas(record.get("datas")) if record else None
        except OdooRecordMissing:
            return None
        except _Undecodable:
            report_skip(SKIP_UNREADABLE)
            return None
        except OdooError as exc:
            if not _is_answer_too_large(exc):
                raise
            report_skip(SKIP_TOO_LARGE)
            return None
        if not data:
            return None
        if len(data) > MAX_BYTES:
            report_skip(SKIP_TOO_LARGE)
            return None
        if mimetype in _TEXT_TYPES:
            content = data.decode("utf-8", errors="replace")
        else:
            content = base64.b64encode(data).decode("ascii")
        if not content.strip():
            return None
        return RawDocument(
            source_uri=_uri(_DOCUMENT, rid),
            title=_text(row.get("name")) or f"Document {rid}",
            content=content,
            content_type=mimetype,
            content_hash=digest,
            metadata={
                "url": conn.client.record_url("documents", rid),
                "app": "documents",
                "path": _m2o_name(row.get("folder_id")),
                "write_date": str(write_date or ""),
            },
        )

    async def _article_document(
        self, conn: _Conn, row: dict[str, Any], known: set[str]
    ) -> RawDocument | None:
        rid: int = row["id"]
        write_date = row.get("write_date")
        digest = _content_hash(_ARTICLE, rid, write_date)
        if digest in known:
            return None
        try:
            record = await _read_one(conn.client, _ARTICLE, rid, ["body"])
        except OdooRecordMissing:
            return None
        except OdooError as exc:
            if not _is_answer_too_large(exc):
                raise
            report_skip(SKIP_TOO_LARGE)
            return None
        body = _text(record.get("body")) if record else ""
        if not body.strip():
            return None
        if len(body.encode()) > MAX_BYTES:
            report_skip(SKIP_TOO_LARGE)
            return None
        return RawDocument(
            source_uri=_uri(_ARTICLE, rid),
            title=_text(row.get("name")) or f"Article {rid}",
            content=body,
            content_type="text/html",
            content_hash=digest,
            metadata={
                "url": conn.client.record_url("knowledge", rid),
                "app": "knowledge",
                "path": _m2o_name(row.get("parent_id")),
                "write_date": str(write_date or ""),
            },
        )

    # -- the picker (SelectableBrowsingConnector) --

    async def browse_selectable(
        self, config: dict[str, Any], path: str, auth: AuthContext | None = None
    ) -> list[BrowseEntry]:
        """Apps, folders and articles the login can see, for the picker.
        Only folders and articles: a source's cut is chosen by those."""
        path = (path or "/").strip()
        async with self._session(config, auth) as conn:
            client = conn.client
            if path in ("", "/"):
                return [
                    await self._app_entry(client, APP_DOCUMENTS, "Documents", _DOCUMENT),
                    await self._app_entry(client, APP_KNOWLEDGE, "Knowledge", _ARTICLE),
                ]
            if path == APP_DOCUMENTS:
                return await self._subfolders(client, None)
            if path == APP_KNOWLEDGE:
                return await self._child_articles(client, None)
            model, rid = _parse_item(path)
            if model == _ARTICLE:
                return await self._child_articles(client, rid)
            if model != self._folder_model(client):
                return []  # a folder id of the other Odoo generation
            return await self._subfolders(client, rid)

    @staticmethod
    async def _app_entry(client: OdooClient, entry_id: str, name: str, model: str) -> BrowseEntry:
        note = None
        if not await client.model_exists(model):
            note = NOTE_APP_MISSING
        elif not await client.can_read(model):
            note = NOTE_NO_ACCESS
        return BrowseEntry(id=entry_id, name=name, is_folder=True, kind="app", note=note)

    @staticmethod
    def _folder_model(client: OdooClient) -> str:
        return _FOLDER_17 if client.version[0] < 18 else _DOCUMENT

    async def _subfolders(self, client: OdooClient, parent: int | None) -> list[BrowseEntry]:
        model = self._folder_model(client)
        if model == _FOLDER_17:
            domain: Domain = [("parent_folder_id", "=", parent or False)]
        else:
            domain = [("type", "=", "folder"), ("folder_id", "=", parent or False)]
        rows = _as_list(
            await client.call(
                model,
                "search_read",
                domain=domain,
                fields=["name"],
                limit=_BROWSE_LIMIT,
                order="name",
            )
        )
        return _entries(rows, model, kind="folder", is_folder=True)

    @staticmethod
    async def _child_articles(client: OdooClient, parent: int | None) -> list[BrowseEntry]:
        rows = _as_list(
            await client.call(
                _ARTICLE,
                "search_read",
                domain=[("parent_id", "=", parent or False), *await _live_articles(client)],
                fields=["name"],
                limit=_BROWSE_LIMIT,
                order="name",
            )
        )
        return _entries(rows, _ARTICLE, kind="article", is_folder=True)

    # -- chat pinning (BrowsingConnector) --

    async def browse(
        self, config: dict[str, Any], path: str, auth: AuthContext | None = None
    ) -> list[BrowseEntry]:
        """Folders and files inside the source's selection only. An article
        is listed as a folder (it has children); inside it, the article itself
        comes first as the file to pin."""
        selection = _parse_selection(config)
        path = (path or "/").strip()
        async with self._session(config, auth) as conn:
            client = conn.client
            if path in ("", "/"):
                return await self._selection_roots(client, selection)
            if path == APP_DOCUMENTS and selection.documents_all:
                return await self._folder_listing(client, None)
            if path == APP_KNOWLEDGE and selection.knowledge_all:
                return await self._child_articles(client, None)
            if path in (APP_DOCUMENTS, APP_KNOWLEDGE):
                raise ConnectorError(_NOT_IN_SCOPE)
            model, rid = _parse_item(path)
            if model == _ARTICLE:
                domains = _article_domains(selection, await _live_articles(client))
                if not await _any_match(client, _ARTICLE, rid, domains):
                    raise ConnectorError(_NOT_IN_SCOPE)
                own = _as_list(await client.call(_ARTICLE, "read", ids=[rid], fields=["name"]))
                itself = _entries(own, _ARTICLE, kind="article", is_folder=False)
                return itself + await self._child_articles(client, rid)
            folder_model, domains = _folder_scope(selection, client.version[0])
            if model != folder_model or not await _any_match(client, model, rid, domains):
                raise ConnectorError(_NOT_IN_SCOPE)
            return await self._folder_listing(client, rid)

    async def _selection_roots(
        self, client: OdooClient, selection: _Selection
    ) -> list[BrowseEntry]:
        out: list[BrowseEntry] = []
        if selection.documents_all:
            out.append(BrowseEntry(id=APP_DOCUMENTS, name="Documents", is_folder=True, kind="app"))
        else:
            model, base = (
                (_FOLDER_17, [])
                if client.version[0] < 18
                else (_DOCUMENT, [("type", "=", "folder")])
            )
            ids = sorted(selection.folders_17 if model == _FOLDER_17 else selection.folders)
            if ids:
                rows = _as_list(
                    await client.call(
                        model,
                        "search_read",
                        domain=[("id", "in", ids), *base],
                        fields=["name"],
                        order="name",
                    )
                )
                out += _entries(rows, model, kind="folder", is_folder=True)
        if selection.knowledge_all:
            out.append(BrowseEntry(id=APP_KNOWLEDGE, name="Knowledge", is_folder=True, kind="app"))
        elif selection.articles:
            rows = _as_list(
                await client.call(
                    _ARTICLE,
                    "search_read",
                    domain=[
                        ("id", "in", sorted(selection.articles)),
                        *await _live_articles(client),
                    ],
                    fields=["name"],
                    order="name",
                )
            )
            out += _entries(rows, _ARTICLE, kind="article", is_folder=True)
        return out

    async def _folder_listing(self, client: OdooClient, folder: int | None) -> list[BrowseEntry]:
        """The subfolders of `folder`, then the files directly in it."""
        entries = await self._subfolders(client, folder)
        if folder is None and client.version[0] < 18:
            return entries  # on Odoo 17 every file sits in a folder
        rows = _as_list(
            await client.call(
                _DOCUMENT,
                "search_read",
                domain=[("folder_id", "=", folder or False), ("type", "!=", "folder")],
                fields=["name", "mimetype", "file_size"],
                limit=_BROWSE_LIMIT,
                order="name",
            )
        )
        for row in rows:
            rid = _record_id(row)
            if rid is None:
                continue
            size = row.get("file_size")
            entries.append(
                BrowseEntry(
                    id=f"{_DOCUMENT}:{rid}",
                    name=_text(row.get("name")),
                    is_folder=False,
                    size=size if isinstance(size, int) else None,
                    content_type=_mimetype(row.get("mimetype")) or None,
                    kind="file",
                )
            )
        return entries

    async def in_scope(
        self, config: dict[str, Any], file_id: str, auth: AuthContext | None = None
    ) -> bool:
        """Whether `file_id` is a file or article the selection covers."""
        try:
            model, rid = _parse_item(file_id)
        except ConnectorError:
            return False
        if model == _FOLDER_17:
            return False
        selection = _parse_selection(config)
        async with self._session(config, auth) as conn:
            return await self._covers(conn.client, selection, model, rid)

    @staticmethod
    async def _covers(client: OdooClient, selection: _Selection, model: str, rid: int) -> bool:
        if model == _DOCUMENT:
            domains = _file_domains(selection, client.version[0])
        elif model == _ARTICLE:
            domains = _article_domains(selection, await _live_articles(client))
        else:
            return False
        return await _any_match(client, model, rid, domains)

    async def fetch_file(
        self,
        config: dict[str, Any],
        file_id: str,
        max_bytes: int,
        auth: AuthContext | None = None,
    ) -> FetchedFile:
        model, rid = _parse_item(file_id)
        selection = _parse_selection(config)
        async with self._session(config, auth) as conn:
            client = conn.client
            if not await self._covers(client, selection, model, rid):
                raise ConnectorError(_NOT_IN_SCOPE)
            if model == _ARTICLE:
                return await self._fetch_article(client, rid, max_bytes)
            return await self._fetch_document(client, rid, max_bytes)

    @staticmethod
    async def _fetch_document(client: OdooClient, rid: int, max_bytes: int) -> FetchedFile:
        meta = await _read_one(client, _DOCUMENT, rid, ["name", "mimetype", "file_size"])
        if not meta:
            raise ConnectorError(_NOT_IN_SCOPE)
        mimetype = _mimetype(meta.get("mimetype"))
        if mimetype not in EXTRACTABLE_TYPES:
            raise ConnectorError("This Odoo file type cannot be opened.")
        size = meta.get("file_size")
        if isinstance(size, int) and size > max_bytes:
            raise ConnectorError(f"The file is too large to open (limit {max_bytes} bytes).")
        record = await _read_one(client, _DOCUMENT, rid, ["datas"])
        data = _decode_datas(record.get("datas")) if record else None
        data = data or b""
        if len(data) > max_bytes:
            raise ConnectorError(f"The file is too large to open (limit {max_bytes} bytes).")
        if mimetype in _TEXT_TYPES:
            text = data.decode("utf-8", errors="replace")
        else:
            text = _extract(base64.b64encode(data).decode("ascii"), mimetype)
        return FetchedFile(
            name=_text(meta.get("name")) or f"Document {rid}",
            content_type=mimetype,
            data=data,
            text=_cap(text, max_bytes),
        )

    @staticmethod
    async def _fetch_article(client: OdooClient, rid: int, max_bytes: int) -> FetchedFile:
        record = await _read_one(client, _ARTICLE, rid, ["name", "body"])
        if not record:
            raise ConnectorError(_NOT_IN_SCOPE)
        data = _text(record.get("body")).encode()
        if len(data) > max_bytes:
            raise ConnectorError(f"The article is too large to open (limit {max_bytes} bytes).")
        text = _extract(data.decode(), "text/html")
        return FetchedFile(
            name=_text(record.get("name")) or f"Article {rid}",
            content_type="text/html",
            data=data,
            text=_cap(text, max_bytes),
        )


def _extract(content: str, content_type: str) -> str:
    """The text of a file, by core's own extraction."""
    from oc8.knowledge.ingest import IngestionError, extract_text

    try:
        return extract_text(content=content, content_type=content_type)
    except IngestionError:
        raise ConnectorError("The Odoo file could not be read.") from None


def _cap(text: str, max_bytes: int) -> str:
    """`text`, cut to at most `max_bytes` of UTF-8."""
    encoded = text.encode()
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def register(contrib: Any) -> None:
    contrib.add_connector(OdooKnowledgeConnector())
