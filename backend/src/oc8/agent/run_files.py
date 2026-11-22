"""Files across the tool boundary, in both directions, for every MCP tool.

A run's files are `FileAttachment` rows (`owner_type="agent_run"`, bytes in
the object store) -- what `write_output_file`, a spilled tool result and a
container's /workspace/output already produce. This module lets a tool server
take part in the same thing instead of pushing bytes through the model:

* **Incoming.** Binary content in a tool result -- an MCP `EmbeddedResource`
  carrying a blob, or `ImageContent` -- is stored as a file of the current
  run, through `store_attachment_bytes` (so the same size cap and type
  allowlist as every other run file), and the model reads a short reference
  in its place: the file id, name, type and size.
* **Outgoing.** A tool marks a parameter with `"x-oc8-file": true` in its
  input schema. The model passes a file id there (or `file:<id>`, or a
  filename); core swaps it for `{file_id, name, mime_type, size,
  content_base64}` before the call leaves, and the marker never reaches the
  model's copy of the schema. A filename names only one of the run's own
  files or the agent's instruction files, and only when it is unique there;
  everything else goes by id, so a file an earlier run kept cannot shadow
  the one meant by carrying its name.

Which run a call belongs to is a context variable, bound by whoever runs the
loop (the in-process engine, the gateway for isolated runtimes), because the
call reaches the MCP session through layers -- routing, pooling, replay --
that know nothing about runs and should not have to.

Which files a run may hand to a tool: its own; those of the same agent's
other runs that are as much its own as it is (Ruling 15, `whose_run`): a
member's run sees her other runs' files, an autonomous run the agent's other
autonomous runs' -- never a colleague's, never across that line; the agent's
instruction files; and the files attached in the run's own chat. Always
within the run's tenant; a file outside that set and a file that does not
exist are refused alike.
"""

from __future__ import annotations

import base64
import binascii
import copy
import logging
import posixpath
import re
import unicodedata
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractAsyncContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlparse

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.db.session import tenant_session
from oc8.runtime.person import (
    SEVERAL_PEOPLE,
    autonomous_sql,
    run_columns,
    run_person_sql,
    whose_run,
)
from oc8.storage import s3
from oc8.storage.attachments import AttachmentRejected, store_attachment_bytes

logger = logging.getLogger(__name__)

#: The schema keyword a tool puts on a parameter that takes a run file.
FILE_MARKER = "x-oc8-file"

#: Appended to a marked parameter's description in the model's copy.
_FILE_HINT = (
    "A file id (from a tool result that kept a file, or write_output_file); "
    "oc8 hands the tool the file itself."
)

#: At most this many file references in one tool call (repeats count), and at
#: most this many bytes of distinct files behind them: one call must not be
#: able to pull an unbounded amount into memory. Both are checked before any
#: file's content is loaded.
MAX_FILE_REFERENCES = 20
MAX_FILE_BYTES_PER_CALL = 50 * 1024 * 1024
#: The other direction: one tool result keeps at most this many files and
#: this many bytes of them; the rest are named "not kept", so a tool cannot
#: fill the object store (or the event loop, extracting text) in one answer.
MAX_KEPT_FILES_PER_RESULT = 20
MAX_KEPT_BYTES_PER_RESULT = 50 * 1024 * 1024

SessionOpener = Callable[[uuid.UUID], AbstractAsyncContextManager[AsyncSession]]


class FileReferenceError(RuntimeError):
    """A file argument core could not resolve; the message is for the model."""


@dataclass(frozen=True)
class RunFiles:
    """The run a tool call belongs to, for keeping and resolving its files."""

    tenant_id: uuid.UUID
    run_id: uuid.UUID
    agent_id: uuid.UUID
    chat_session_id: uuid.UUID | None = None
    #: Its own transaction per file: calls may run concurrently (a batch of
    #: reads), and a run's own session must never be shared between them.
    open_session: SessionOpener = field(default=tenant_session, compare=False)

    @classmethod
    def of(cls, run: m.AgentRun) -> RunFiles:
        raw = (run.context or {}).get("chat_session_id")
        try:
            chat = uuid.UUID(str(raw)) if raw else None
        except ValueError:
            chat = None
        return cls(
            tenant_id=run.tenant_id, run_id=run.id, agent_id=run.agent_id, chat_session_id=chat
        )

    async def keep(self, *, name: str, content_type: str, raw: bytes) -> str:
        """Store `raw` as a file of this run; the reference the model reads."""
        try:
            async with self.open_session(self.tenant_id) as db:
                row = await store_attachment_bytes(
                    db,
                    tenant_id=self.tenant_id,
                    owner_type="agent_run",
                    owner_id=self.run_id,
                    filename=name,
                    raw=raw,
                    content_type=content_type,
                )
                file_id = row.id
        except AttachmentRejected as exc:
            return _not_kept(name, content_type, len(raw), str(exc))
        except Exception:
            logger.exception("run %s: could not keep tool file %r", self.run_id, name)
            return _not_kept(name, content_type, len(raw), "storing it failed")
        return (
            f'[File kept for this run: "{name}" ({content_type}, {_size(len(raw))}), '
            f"file id {file_id}. Pass this id where a tool takes a file.]"
        )

    async def resolve_many(self, refs: list[str]) -> list[dict[str, Any]]:
        """The files `refs` name, in order, as a tool receives them. Each
        distinct file is loaded and encoded once, however often it is named."""
        if len(refs) > MAX_FILE_REFERENCES:
            raise FileReferenceError(
                f"A tool call may name at most {MAX_FILE_REFERENCES} files; this one names "
                f"{len(refs)}."
            )
        async with self.open_session(self.tenant_id) as db:
            rows: list[m.FileAttachment] = []
            for ref in refs:
                row = await self._find(db, ref)
                if row is None:
                    raise FileReferenceError(
                        f"There is no file {ref!r} this run can use. Pass the file id a "
                        "tool result or write_output_file gave you."
                    )
                rows.append(row)
        distinct = {row.id: row for row in rows}
        total = sum(row.size_bytes for row in distinct.values())
        if total > MAX_FILE_BYTES_PER_CALL:
            raise FileReferenceError(
                f"The files of one tool call may be at most {_size(MAX_FILE_BYTES_PER_CALL)} "
                f"together; these are {_size(total)}."
            )
        loaded: dict[uuid.UUID, dict[str, Any]] = {}
        for file_id, row in distinct.items():
            data = await s3.get_object(row.bucket_key)
            loaded[file_id] = {
                "file_id": str(row.id),
                "name": row.filename,
                "mime_type": row.content_type,
                "size": row.size_bytes,
                "content_base64": base64.b64encode(data).decode(),
            }
        return [loaded[row.id] for row in rows]

    async def _runs_alike(self, db: AsyncSession) -> Any:
        """The runs whose files this run may use: itself, and the agent's other
        runs that are the same person's -- or, for an autonomous run, the
        agent's other autonomous runs (Ruling 15). A run that is nobody's own
        and not autonomous either (several people, an unreadable account) sees
        its own files only."""
        run = (
            await db.execute(
                select(m.AgentRun).where(
                    m.AgentRun.id == self.run_id, m.AgentRun.tenant_id == self.tenant_id
                )
            )
        ).scalar_one_or_none()
        if run is None or run.agent_id != self.agent_id:
            return None
        whose = await whose_run(db, run)
        cols = run_columns()
        if whose.autonomous:
            alike = autonomous_sql(cols)
        elif whose.person and whose.person != SEVERAL_PEOPLE:
            alike = run_person_sql(cols) == whose.person
        else:
            return None
        return select(m.AgentRun.id).where(
            m.AgentRun.tenant_id == self.tenant_id, m.AgentRun.agent_id == self.agent_id, alike
        )

    async def _find(self, db: AsyncSession, ref: str) -> m.FileAttachment | None:
        file_id = _file_id(ref)
        if file_id is None:
            return await self._find_by_name(db, ref.strip())
        of_run = m.FileAttachment.owner_type == "agent_run"
        alike = await self._runs_alike(db)
        visible = [
            of_run
            & (
                (m.FileAttachment.owner_id == self.run_id)
                if alike is None
                else or_(
                    m.FileAttachment.owner_id == self.run_id, m.FileAttachment.owner_id.in_(alike)
                )
            ),
            (m.FileAttachment.owner_type == "agent_instructions")
            & (m.FileAttachment.owner_id == self.agent_id),
        ]
        if self.chat_session_id is not None:
            messages = select(m.ChatMessage.id).where(
                m.ChatMessage.tenant_id == self.tenant_id,
                m.ChatMessage.session_id == self.chat_session_id,
            )
            visible.append(
                (m.FileAttachment.owner_type == "chat_message")
                & (
                    (m.FileAttachment.owner_id == self.chat_session_id)
                    | m.FileAttachment.owner_id.in_(messages)
                )
            )
        query = select(m.FileAttachment).where(
            m.FileAttachment.tenant_id == self.tenant_id, or_(*visible)
        )
        found: m.FileAttachment | None = (
            await db.execute(query.where(m.FileAttachment.id == file_id))
        ).scalar_one_or_none()
        return found

    async def _find_by_name(self, db: AsyncSession, name: str) -> m.FileAttachment | None:
        # A name is looked up among this run's own files and the agent's
        # instructions only: anything else could carry a chosen name (a mail
        # attachment kept by an earlier run) and shadow the file meant. Names
        # are not unique, so two matches are refused rather than guessed.
        named = (
            (
                await db.execute(
                    select(m.FileAttachment)
                    .where(
                        m.FileAttachment.tenant_id == self.tenant_id,
                        m.FileAttachment.filename == name,
                        or_(
                            (m.FileAttachment.owner_type == "agent_run")
                            & (m.FileAttachment.owner_id == self.run_id),
                            (m.FileAttachment.owner_type == "agent_instructions")
                            & (m.FileAttachment.owner_id == self.agent_id),
                        ),
                    )
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
        if len(named) > 1:
            raise FileReferenceError(f"Several files are named {name!r}; pass the file id.")
        return named[0] if named else None


_CURRENT: ContextVar[RunFiles | None] = ContextVar("oc8_run_files", default=None)


@contextmanager
def bind_run_files(files: RunFiles | None) -> Iterator[None]:
    """Tool calls made inside this block belong to `files`' run."""
    token = _CURRENT.set(files)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_run_files() -> RunFiles | None:
    return _CURRENT.get()


# --- schemas -------------------------------------------------------------------


@dataclass(frozen=True)
class AnyKey:
    """A path step into every value of an object (`additionalProperties`)
    except those its declared `properties` name."""

    declared: frozenset[str] = frozenset()


#: One step of a path: a property name, "*" for every array item, an index
#: for one item (`prefixItems`), or a step into a map's other values.
#: `patternProperties` is not followed: that would run the tool server's
#: regex on the event loop (a catastrophic one holds it). A file marked
#: there is not resolved; the tool gets the bare id and refuses it, and
#: `model_schema` still strips the marker.
PathStep = str | int | AnyKey
#: A path to a marked value.
FilePath = tuple[PathStep, ...]

#: A recursive schema is followed this deep, no further, and a `$ref`
#: already on the way there is never entered again. All of one schema's walk
#: visits at most `_MAX_SCHEMA_VISITS` nodes: it runs while the tool list is
#: read, and a schema of mutually referring definitions would otherwise take
#: exponential time. A file behind either limit is not swapped; the tool gets
#: the model's text as given and refuses it as no file (fails closed), and
#: `model_schema` still strips its marker.
_MAX_SCHEMA_DEPTH = 12
_MAX_SCHEMA_VISITS = 10_000

#: Keywords whose value is one subschema, or a list of them, at the same
#: place in the instance (no path step).
_IN_PLACE = ("anyOf", "oneOf", "allOf", "if", "then", "else")
#: Keywords whose value is data, never a schema: `model_schema` leaves them.
_DATA_KEYWORDS = frozenset({"const", "default", "enum", "examples", "required"})
#: Keywords whose value maps names to subschemas: the names are not keywords.
_SCHEMA_MAPS = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
)


def _children(node: Mapping[str, Any]) -> Iterator[tuple[Any, PathStep | None]]:
    """The subschemas of `node` that apply to its instance or a value in it,
    each with the path step that reaches that value (None: the same value).
    The one list of keywords both `file_paths` and `model_schema` follow."""
    for key in _IN_PLACE:
        sub = node.get(key)
        for item in sub if isinstance(sub, list) else [sub]:
            yield item, None
    properties = node.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    for name, sub in properties.items():
        yield sub, str(name)
    extra = node.get("additionalProperties")
    if isinstance(extra, Mapping):
        yield extra, AnyKey(frozenset(map(str, properties)))
    prefix = node.get("prefixItems")
    if isinstance(prefix, list):
        for index, sub in enumerate(prefix):
            yield sub, index
    items = node.get("items")
    if isinstance(items, Mapping):
        yield items, "*"
    elif isinstance(items, list):  # draft 4-7 tuple form
        for index, sub in enumerate(items):
            yield sub, index


def _ref(root: Mapping[str, Any], ref: Any) -> Mapping[str, Any] | None:
    """A local `$ref` (`#/$defs/X`, `#/definitions/X`, any `#/...` pointer)
    resolved against `root`; None for a remote or broken one."""
    if not isinstance(ref, str) or not ref.startswith("#"):
        return None
    node: Any = root
    for part in [p for p in ref[1:].split("/") if p]:
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, Mapping) else None


def file_paths(schema: Mapping[str, Any]) -> list[FilePath]:
    """Where `schema` takes a run file (`_children`, and local `$ref`s)."""
    out: set[FilePath] = set()
    visits = 0
    exhausted = False

    def walk(node: Any, path: FilePath, depth: int, refs: frozenset[str]) -> None:
        nonlocal visits, exhausted
        if exhausted or not isinstance(node, Mapping) or depth > _MAX_SCHEMA_DEPTH:
            return
        visits += 1
        if visits > _MAX_SCHEMA_VISITS:
            exhausted = True
            logger.warning(
                "a tool schema exceeds the walk budget of %d nodes; file parameters "
                "beyond it are not resolved",
                _MAX_SCHEMA_VISITS,
            )
            return
        if node.get(FILE_MARKER) is True:
            if path:
                out.add(path)
            return
        ref = node.get("$ref")
        if isinstance(ref, str) and ref not in refs:
            target = _ref(schema, ref)
            if target is not None:
                walk(target, path, depth + 1, refs | {ref})
        for sub, step in _children(node):
            walk(sub, path if step is None else (*path, step), depth + 1, refs)

    walk(schema, (), 0, frozenset())
    return sorted(out, key=repr)


def model_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """`schema` as the model sees it: no marker anywhere, a hint in its place.

    Every schema in it, reachable by `file_paths` or not ($defs nobody
    references, nodes past its limits), loses the `x-oc8-file` keyword; a
    property that happens to be named so, and data such as `const`, are left
    alone. No `$ref` is followed, so each node is seen once."""
    copied = copy.deepcopy(dict(schema))

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for sub in node:
                walk(sub)
            return
        if not isinstance(node, dict):
            return
        if node.pop(FILE_MARKER, None) is True:
            given = str(node.get("description") or "").strip()
            node["description"] = f"{given} {_FILE_HINT}".strip()
        for key, value in node.items():
            if key in _DATA_KEYWORDS:
                continue
            if key in _SCHEMA_MAPS and isinstance(value, dict):
                for sub in value.values():
                    walk(sub)
            else:
                walk(value)

    walk(copied)
    return copied


async def resolve_file_arguments(
    arguments: dict[str, Any], paths: list[FilePath], files: RunFiles | None
) -> dict[str, Any]:
    """`arguments` with every file reference at `paths` replaced by the file."""
    if not paths:
        return arguments
    resolved = copy.deepcopy(arguments)
    references: list[tuple[Any, str | int, Any]] = []

    def collect(node: Any, path: FilePath, parent: Any, key: str | int) -> None:
        if not path:
            if node is not None and node != "":
                references.append((parent, key, node))
            return
        head, rest = path[0], path[1:]
        if isinstance(head, int):
            if isinstance(node, list) and head < len(node):
                collect(node[head], rest, node, head)
        elif isinstance(head, AnyKey):
            if isinstance(node, dict):
                for name in list(node):
                    if name not in head.declared:
                        collect(node[name], rest, node, name)
        elif head == "*":
            if isinstance(node, list):
                for index, item in enumerate(node):
                    collect(item, rest, node, index)
        elif isinstance(node, dict) and head in node:
            collect(node[head], rest, node, head)

    for path in paths:
        collect(resolved, path, None, "")
    if not references:
        return arguments
    if files is None:
        raise FileReferenceError(
            "This tool takes a file, and files only exist within an agent run."
        )
    refs: list[str] = []
    for _, _, value in references:
        if not isinstance(value, str):
            raise FileReferenceError("Pass a file as its file id (text).")
        refs.append(value)
    for (parent, key, _), file in zip(references, await files.resolve_many(refs), strict=True):
        parent[key] = file
    return resolved


# --- tool results ----------------------------------------------------------------


def binary_of(block: Any, index: int) -> tuple[str, str, bytes] | None:
    """`(name, content type, bytes)` for a binary content block, else None.

    Attribute names differ between SDK majors (`mime_type` in 2.x, `mimeType`
    in 1.x); both are read."""
    kind = getattr(block, "type", None)
    if kind == "image":
        data, mime, name = getattr(block, "data", None), _mime(block), ""
    elif kind == "resource":
        resource = getattr(block, "resource", None)
        data = getattr(resource, "blob", None)
        mime, name = _mime(resource), _name_from_uri(str(getattr(resource, "uri", "") or ""))
    else:
        return None
    if not isinstance(data, str):
        return None
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None
    mime = mime or "application/octet-stream"
    if not name:
        name = f"{kind}-{index}{_extension(mime)}"
    return name, mime, raw


async def render_content(blocks: list[Any]) -> str:
    """A tool result's content as the text the model reads."""
    files = current_run_files()
    parts: list[str] = []
    kept_files = kept_bytes = 0
    for index, block in enumerate(blocks):
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
            continue
        resource_text = getattr(getattr(block, "resource", None), "text", None)
        if isinstance(resource_text, str):
            parts.append(resource_text)
            continue
        found = binary_of(block, index)
        if found is None:
            continue
        name, mime, raw = found
        if files is None:
            parts.append(_not_kept(name, mime, len(raw), "this call is not part of an agent run"))
        elif (
            kept_files >= MAX_KEPT_FILES_PER_RESULT
            or kept_bytes + len(raw) > MAX_KEPT_BYTES_PER_RESULT
        ):
            parts.append(
                _not_kept(
                    name,
                    mime,
                    len(raw),
                    f"limit (one tool result keeps at most {MAX_KEPT_FILES_PER_RESULT} "
                    f"files, {_size(MAX_KEPT_BYTES_PER_RESULT)} together)",
                )
            )
        else:
            kept_files += 1
            kept_bytes += len(raw)
            parts.append(await files.keep(name=name, content_type=mime, raw=raw))
    return "\n".join(parts)


def _not_kept(name: str, mime: str, size: int, why: str) -> str:
    return f'[File "{name}" ({mime}, {_size(size)}) not kept: {why}.]'


#: A media type as RFC 6838 allows it (`type/subtype`, restricted names);
#: anything else a tool claims is echoed as octet-stream, never verbatim.
_MEDIA_TYPE = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}", re.ASCII
)


def _mime(obj: Any) -> str:
    raw = getattr(obj, "mime_type", None) or getattr(obj, "mimeType", None) or ""
    mime = str(raw).split(";", 1)[0].strip().lower()
    if not mime:
        return ""
    return mime if _MEDIA_TYPE.fullmatch(mime) else "application/octet-stream"


#: Characters a kept file's name loses: path separators, and what could
#: make the reference line the model reads say something else -- quotes and
#: brackets to break out of it, invisible format characters (a right-to-left
#: override turns "fdp.exe" into what reads as "exe.pdf"), line breaks.
_NAME_PUNCTUATION = frozenset("/\\\"'`()[]{}<>")
_NAME_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def _safe_name(name: str) -> str:
    cleaned = "".join(
        "_" if ch in _NAME_PUNCTUATION or unicodedata.category(ch) in _NAME_CATEGORIES else ch
        for ch in name
    )
    return cleaned.strip()[:200]


def _name_from_uri(uri: str) -> str:
    path = urlparse(uri).path or uri
    return _safe_name(posixpath.basename(unquote(path).rstrip("/")))


_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}


def _extension(mime: str) -> str:
    return _EXTENSIONS.get(mime, "")


def _file_id(ref: str) -> uuid.UUID | None:
    text = ref.strip()
    if text.startswith("file:"):
        text = text[len("file:") :]
    try:
        return uuid.UUID(text)
    except ValueError:
        return None


def _size(n: int) -> str:
    if n < 1024:
        return f"{n} bytes"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"
