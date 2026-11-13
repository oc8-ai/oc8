"""Links into the systems behind a capa connection (spec
2026-11-13-record-links-design.md §4.2).

Core still knows no vendor. A capa's `record_url` declaration says where its
base URL lives, which entities are linkable, which extra placeholders a URL
needs and where their values come from, and which result fields already hold
the system's own link. This module only evaluates that declaration.

EVERY failure is "no link": `None`, an empty list, or the output unchanged.
This runs inside live tool calls and approval raises; a cosmetic link that
raised would turn a governed call into a failed run.
"""

from __future__ import annotations

import json
import logging
import posixpath
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from string import Formatter
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.capas.manifest import RECORD_URL_BUILTIN_PLACEHOLDERS, RecordUrlTemplate

logger = logging.getLogger(__name__)

_MAX_URL_CHARS = 2000
MAX_LINKS_PER_RESULT = 50
#: Where a list of records sits inside a result object. One level only: a
#: record's own nested objects (an issue's author, a deal's owner) are not the
#: records the model was asked about.
_CONTAINER_KEYS = (
    "records",
    "results",
    "items",
    "issues",
    "value",
    "data",
    "files",
    "messages",
    "objects",
)
#: Where a SINGLE record sits inside a result that has no id of its own:
#: mcp-server-odoo answers create/update/get with {success, record: {...},
#: url, message}. Also one level only.
_WRAPPER_KEYS = ("record", "result", "data", "item", "object")


@dataclass(frozen=True)
class LinkContext:
    spec: RecordUrlTemplate
    focus_spec: dict[str, Any] | None
    config: Mapping[str, Any]
    #: The connection's RESOLVED launch env. A credential's URL (a personal
    #: Odoo or Jira login) lands here, not in `config["env"]`, which only holds
    #: the pack's defaults.
    env: Mapping[str, str]
    labels: Mapping[str, str]
    #: The manifest's own `connections.config` for this connection: the last
    #: fallback for a value the row and the launch env both lack (an optional
    #: login field left blank must not erase the pack's default, e.g. HubSpot's
    #: US app address).
    defaults: Mapping[str, Any] = field(default_factory=dict)


def safe_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url.startswith(("http://", "https://")) or len(url) > _MAX_URL_CHARS:
        return None
    return url


def link_context_from(
    spec: RecordUrlTemplate | None,
    *,
    config: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
    defaults: Mapping[str, Any] | None = None,
) -> LinkContext | None:
    if spec is None:
        return None
    focus = config.get("focus_spec") if isinstance(config.get("focus_spec"), dict) else None
    labels = (focus or {}).get("labels") or {}
    return LinkContext(
        spec=spec,
        focus_spec=focus,
        config=config,
        env=env or {},
        labels={str(k): str(v) for k, v in labels.items()} if isinstance(labels, dict) else {},
        defaults=defaults or {},
    )


def link_context(
    conn: m.McpConnection | None, env: Mapping[str, str] | None = None
) -> LinkContext | None:
    """The context for a LIVE connection row. The declaration is read off the
    plugin manifest on disk, never off the row (see `RecordUrlTemplate`)."""
    if conn is None:
        return None
    try:
        from oc8.capas.discovery import resolve_tool_pack_connection

        cfg = conn.config if isinstance(conn.config, dict) else {}
        declared = resolve_tool_pack_connection(
            str(cfg.get("_plugin_name", "")), str(cfg.get("_connection_key", ""))
        )
        return link_context_from(
            declared.record_url if declared else None,
            config=cfg,
            env=env,
            defaults=declared.config if declared else None,
        )
    except Exception:  # never let a link cost a call
        logger.warning("record link context for %s could not be built", conn.name, exc_info=True)
        return None


def _dig(node: Any, path: list[str]) -> Any:
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def _text(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (str, int)):
        return str(value).strip()
    return ""


def config_path(ctx: LinkContext, path: list[str]) -> str:
    if len(path) == 2 and path[0] == "env":
        resolved = _text(ctx.env.get(path[1]))
        if resolved:
            return resolved
    return _text(_dig(ctx.config, path)) or _text(_dig(ctx.defaults, path))


def _host_allowed(url: str, ctx: LinkContext) -> bool:
    """A native URL is trusted only on the connection's own host or a declared
    one: a result is untrusted text and an issued link is whitelisted in answers."""
    from oc8.records.registry import host_of

    # Browsers read `\` as `/`; no legitimate native link carries one, so fail closed.
    if "\\" in url:
        return False
    host = host_of(url)
    if not host:
        return False
    spec = ctx.spec
    base = config_path(ctx, list(spec.base_url_path)) if spec.base_url_path else ""
    if base and host == host_of(base):
        return True
    for allowed in spec.native_url_hosts:
        if "/" in allowed:
            # `host/path-prefix`: a shared host (www.google.com also serves a
            # redirector) is trusted for one path only. Compare the decoded,
            # dot-segment-normalised path so `/calendar/../url` cannot pass.
            allowed_host, _, prefix = allowed.partition("/")
            path = posixpath.normpath(unquote(urlsplit(url.replace("\\", "/")).path))
            if host == allowed_host.lower() and (path + "/").startswith(
                "/" + prefix.rstrip("/") + "/"
            ):
                return True
        elif allowed.startswith("*."):
            if host.endswith(allowed[1:].lower()) and len(host) > len(allowed) - 1:
                return True
        elif host == allowed.lower():
            return True
    return False


def _placeholder(
    ctx: LinkContext, sources: list[str], args: Mapping[str, Any], item: Mapping[str, Any]
) -> str:
    for source in sources:
        kind, _, path = source.partition(":")
        keys = [p for p in path.split(".") if p]
        if kind == "arg":
            value = _text(_dig(args, keys))
        elif kind == "item":
            value = _text(_dig(item, keys))
        elif kind == "config":
            value = config_path(ctx, keys)
        else:
            value = ""
        if value:
            return value
    return ""


def link_for(
    ctx: LinkContext | None,
    *,
    entity: str,
    ref: str,
    args: Mapping[str, Any] | None = None,
    item: Mapping[str, Any] | None = None,
) -> str | None:
    """One record's URL: the system's own link from `item` when declared and
    present, else the template for `entity`. None for anything unresolvable."""
    if ctx is None:
        return None
    try:
        spec = ctx.spec
        if item is not None:
            for field_name in spec.native_url_fields:
                native = safe_url(item.get(field_name))
                if native and _host_allowed(native, ctx):
                    return native
        template = spec.templates_for(entity) if entity else ""
        segment = spec.models.get(entity) if entity else None
        if not template or not segment or not ref:
            return None
        names = {n for _lit, n, _spec, _conv in Formatter().parse(template) if n}
        values: dict[str, str] = {"model": quote(segment, safe=""), "id": quote(ref, safe="")}
        if "base_url" in names:
            base = config_path(ctx, list(spec.base_url_path))
            if not base:
                return None
            values["base_url"] = base.rstrip("/")
        for name in names - RECORD_URL_BUILTIN_PLACEHOLDERS:
            value = _placeholder(ctx, spec.placeholders.get(name, []), args or {}, item or {})
            if not value:
                return None
            values[name] = quote(value, safe="")
        return safe_url(template.format(**values))
    except Exception:
        logger.warning("record link for %s could not be built", entity, exc_info=True)
        return None


def _item_ref(item: Mapping[str, Any], focus_spec: dict[str, Any] | None) -> str:
    keys: list[str] = []
    if focus_spec:
        if focus_spec.get("id_field"):
            keys.append(str(focus_spec["id_field"]))
        keys.extend(str(k) for k in focus_spec.get("id_fields") or [])
    keys.append("id")
    for key in keys:
        value = _text(item.get(key))
        if value:
            return value
    return ""


def _candidates(parsed: Any, focus_spec: dict[str, Any] | None) -> Iterator[dict[str, Any]]:
    if isinstance(parsed, list):
        yield from (x for x in parsed if isinstance(x, dict))
        return
    if not isinstance(parsed, dict):
        return
    for key in _CONTAINER_KEYS:
        inner = parsed.get(key)
        if isinstance(inner, list) and any(isinstance(x, dict) for x in inner):
            yield from (x for x in inner if isinstance(x, dict))
            return
    if not _item_ref(parsed, focus_spec):
        # Only an unambiguous wrapper: two candidate dicts could be a record
        # and its context, and guessing would link the wrong one.
        wrapped = [v for k in _WRAPPER_KEYS if isinstance(v := parsed.get(k), dict)]
        if len(wrapped) == 1:
            yield wrapped[0]
            return
    yield parsed


def enrich(
    ctx: LinkContext | None, tool_name: str, arguments: Mapping[str, Any], output: str
) -> tuple[str, list[str]]:
    """`output` with `oc8_url` set on each record it holds, and the URLs issued.

    Only JSON results are touched, and only when at least one link resolved --
    otherwise the exact original string comes back, so an unconfigured
    connection changes nothing about what the model sees.
    """
    if ctx is None:
        return output, []
    try:
        parsed = json.loads(output)
    except (ValueError, TypeError, RecursionError):
        return output, []
    try:
        # Lazy: oc8.approvals.record_url imports this module and the agent
        # package imports approvals, so a module-level import is a cycle.
        from oc8.agent.tool_semantics import call_entity

        entity = call_entity(tool_name, dict(arguments), ctx.focus_spec)
        issued: list[str] = []
        for item in _candidates(parsed, ctx.focus_spec):
            if len(issued) >= MAX_LINKS_PER_RESULT:
                break
            url = link_for(
                ctx, entity=entity, ref=_item_ref(item, ctx.focus_spec), args=arguments, item=item
            )
            if url:
                item["oc8_url"] = url
                issued.append(url)
        if not issued:
            return output, []
        return json.dumps(parsed, ensure_ascii=False), issued
    except Exception:
        logger.warning("record links for %s could not be added", tool_name, exc_info=True)
        return output, []


def related_links(ctx: LinkContext | None, arguments: Mapping[str, Any]) -> list[dict[str, str]]:
    """Links to the EXISTING records a call references by argument -- the
    customer of a quotation that does not exist yet (spec §5 item 5).

    Labelled by entity and id only. A many2one's display name ([7, "XYZ GmbH"])
    is model-supplied text: shown on the approval card it could name a trusted
    customer while the link opens record 7, so an approver would be misled."""
    if ctx is None:
        return []
    out: list[dict[str, str]] = []
    for path, entity in ctx.spec.related_fields.items():
        try:
            raw = _dig(arguments, [p for p in path.split(".") if p])
            if isinstance(raw, list) and raw:
                # Odoo many2one as read back: [id, "display name"]. The id only.
                raw = raw[0]
            ref = _text(raw)
            url = link_for(ctx, entity=entity, ref=ref, args=arguments) if ref else None
            if url:
                label = ctx.labels.get(entity, entity)
                out.append({"label": f"{label} #{ref}", "url": url})
        except Exception:
            logger.warning("related link %s could not be built", path, exc_info=True)
    return out


async def enrich_and_remember(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    scope: str,
    ctx: LinkContext | None,
    tool_name: str,
    arguments: Mapping[str, Any],
    output: str,
) -> tuple[str, list[str]]:
    """`enrich`, plus remembering what it issued in the registry under the
    calling run's `scope` (`registry.link_scope`), so the answer check later
    keeps exactly these links, in that conversation (spec §4.2, §5 items 1 and 3).

    A replayed write comes back as the first result with the idempotency note
    appended, which is no longer JSON. The JSON part is enriched and the note
    put back, so a resumed run sees the same links the first one did.
    """
    if ctx is None:
        return output, []
    # Lazy for the same cycle as `call_entity` in `enrich`.
    from oc8.agent.tool_idempotency import REPLAY_NOTE
    from oc8.records.registry import remember_links

    note = f"\n\n{REPLAY_NOTE}"
    body, suffix = (output[: -len(note)], note) if output.endswith(note) else (output, "")
    new, issued = enrich(ctx, tool_name, arguments, body)
    if issued:
        try:
            async with db.begin_nested():
                await remember_links(db, tenant_id=tenant_id, scope=scope, urls=issued)
        except Exception:
            # An unremembered link is unlinked in the answer later; that is
            # better than failing the call over it. The model still sees it.
            logger.warning("issued record links could not be remembered", exc_info=True)
    return new + suffix, issued
