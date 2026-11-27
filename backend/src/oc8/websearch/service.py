"""Which search provider a tenant uses, and the search itself.

Resolution order: the tenant's own setting, else the platform default
(`OC8_WEB_SEARCH_*`), else no web search. A tenant can also switch it off for
itself (`provider: "off"`), which wins over the platform default.

The tenant setting lives in `organization.settings["web_search"]`
(`{"provider", "base_url"}`); its API key is a tenant secret
(`oc8.secrets`, encrypted at rest) under `API_KEY_SECRET`, never in the
settings row and never returned by the API.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.config import get_settings
from oc8.secrets.service import SecretNotFound, delete_secret, resolve_secret, store_secret
from oc8.websearch.providers import (
    KEYED_PROVIDERS,
    PROVIDERS,
    ProviderConfig,
)

#: The key in `organization.settings`.
SETTINGS_KEY = "web_search"
#: The tenant secret holding the tenant's own API key.
API_KEY_SECRET = "web_search/api_key"
#: The run context key a chat turn sets when the person switched "Web" on.
RUN_CONTEXT_KEY = "web_search"

Source = Literal["tenant", "platform"]


@dataclass(frozen=True)
class TenantSetting:
    """What the tenant chose: a provider, "off", or nothing (None)."""

    provider: str | None
    base_url: str | None
    has_key: bool


#: A base URL's path: plain segments only (letters, digits, `-._~`), never
#: `.` or `..`, nothing percent-encoded.
_PATH = re.compile(r"(/[A-Za-z0-9._~-]+)*/?")
_UNPRINTABLE = re.compile(r"[\x00-\x20\x7f-\x9f\u2028\u2029]")


def base_url_problem(raw: str) -> str | None:
    """Why `raw` is no plain SearXNG base URL, or None when it is one: http(s),
    a host, a readable port in range, no login, no query or fragment, no
    whitespace or control characters, and a path of plain segments only."""
    if not raw or _UNPRINTABLE.search(raw):
        return "The address must not contain spaces or control characters."
    try:
        parts = urlsplit(raw)
        _ = parts.port  # raises ValueError for a port that is no number or out of range
        host = parts.hostname
    except ValueError:
        return "The address cannot be read."
    if parts.scheme not in ("http", "https") or not host:
        return "SearXNG needs its address, starting with http:// or https://."
    if parts.username is not None or parts.password is not None or "\\" in raw:
        return "The address must not contain a login."
    if parts.query or parts.fragment or "?" in raw or "#" in raw:
        return "The address must not contain a query or a fragment."
    path = parts.path
    if not _PATH.fullmatch(path) or any(seg in (".", "..") for seg in path.split("/")):
        return "The address's path must be plain, e.g. https://search.example.org/searx."
    return None


def platform_config() -> ProviderConfig | None:
    """The operator's default provider, or None when there is none (or it is
    incomplete: SearXNG without a URL, Brave/Tavily without a key)."""
    settings = get_settings()
    provider = settings.web_search_provider.strip().lower()
    if provider not in PROVIDERS:
        return None
    base_url = settings.web_search_base_url.strip() or None
    api_key = settings.web_search_api_key.strip() or None
    if provider == "searxng":
        if base_url is None:
            return None
        return ProviderConfig(
            provider="searxng",
            base_url=base_url,
            internal_host=urlsplit(base_url).hostname or None,
        )
    if api_key is None:
        return None
    return ProviderConfig(provider=provider, api_key=api_key)  # type: ignore[arg-type]


def _stored(org: m.Organization | None) -> Mapping[str, Any]:
    raw = (org.settings or {}).get(SETTINGS_KEY) if org is not None else None
    return raw if isinstance(raw, Mapping) else {}


async def _has_key(db: AsyncSession, *, tenant_id: uuid.UUID) -> bool:
    found = await db.scalar(
        select(m.Secret.id).where(m.Secret.tenant_id == tenant_id, m.Secret.name == API_KEY_SECRET)
    )
    return found is not None


async def tenant_setting(db: AsyncSession, *, tenant_id: uuid.UUID) -> TenantSetting:
    stored = _stored(await db.get(m.Organization, tenant_id))
    provider = stored.get("provider")
    base_url = stored.get("base_url")
    return TenantSetting(
        provider=provider if isinstance(provider, str) else None,
        base_url=base_url if isinstance(base_url, str) and base_url else None,
        has_key=await _has_key(db, tenant_id=tenant_id),
    )


async def save_tenant_setting(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    provider: str | None,
    base_url: str | None,
    api_key: str | None,
) -> None:
    """Store the tenant's choice. The caller has validated it. A new key
    replaces the stored one; a provider that needs none drops it."""
    org = await db.get(m.Organization, tenant_id)
    if org is None:
        raise LookupError("organization not found")
    settings = dict(org.settings or {})
    if provider is None:
        settings.pop(SETTINGS_KEY, None)
    else:
        entry: dict[str, Any] = {"provider": provider}
        if provider == "searxng" and base_url:
            entry["base_url"] = base_url
        settings[SETTINGS_KEY] = entry
    org.settings = settings
    if provider in KEYED_PROVIDERS and api_key:
        await store_secret(
            db, tenant_id=tenant_id, name=API_KEY_SECRET, value=api_key, kind="credential"
        )
    elif provider not in KEYED_PROVIDERS:
        try:
            await delete_secret(db, tenant_id=tenant_id, ref=API_KEY_SECRET)
        except SecretNotFound:
            pass
    await db.flush()


async def effective_source(db: AsyncSession, *, tenant_id: uuid.UUID) -> Source | None:
    """Where this tenant's web search comes from, or None when it has none."""
    setting = await tenant_setting(db, tenant_id=tenant_id)
    if setting.provider is not None:
        return "tenant" if setting.provider in PROVIDERS else None
    return "platform" if platform_config() is not None else None


async def resolve_config(db: AsyncSession, *, tenant_id: uuid.UUID) -> ProviderConfig | None:
    """The provider this tenant's searches go to, or None."""
    setting = await tenant_setting(db, tenant_id=tenant_id)
    if setting.provider is None:
        return platform_config()
    if setting.provider not in PROVIDERS:
        # "off", or something this version does not know: no search.
        return None
    if setting.provider == "searxng":
        if setting.base_url is None:
            return None
        return ProviderConfig(provider="searxng", base_url=setting.base_url)
    try:
        key = await resolve_secret(db, tenant_id=tenant_id, ref=API_KEY_SECRET)
    except SecretNotFound:
        return None
    return ProviderConfig(provider=setting.provider, api_key=key)  # type: ignore[arg-type]


async def is_available(db: AsyncSession, *, tenant_id: uuid.UUID) -> bool:
    return await resolve_config(db, tenant_id=tenant_id) is not None


def requested(context: Mapping[str, Any] | None) -> bool:
    """Whether this run's turn asked for web search (`web: true` on the message)."""
    return (context or {}).get(RUN_CONTEXT_KEY) is True


async def offered(
    db: AsyncSession, *, tenant_id: uuid.UUID, context: Mapping[str, Any] | None
) -> bool:
    """Whether `web_search` is offered on this run: asked for AND available."""
    return requested(context) and await is_available(db, tenant_id=tenant_id)


#: A citation in an answer, as the chat renders it (`chat-markdown.tsx`):
#: `[3]`, not a Markdown link `[3](...)`.
_CITATION = re.compile(r"\[(\d{1,2})\](?!\()")


def cited_sources(hits: object, answer: str) -> list[dict[str, Any]]:
    """The web hits `answer` cites by number, as chat sources of kind `web`
    (`ChatSourceDTO`), in their numbers' order. `hits` is the run's
    `web_search_hits` list; anything malformed in it is skipped."""
    if not isinstance(hits, list) or not answer:
        return []
    cited = {int(n) for n in _CITATION.findall(answer)}
    out: list[dict[str, Any]] = []
    for hit in hits:
        if not isinstance(hit, Mapping):
            continue
        n, url = hit.get("n"), hit.get("url")
        if not isinstance(n, int) or n not in cited or not isinstance(url, str):
            continue
        out.append(
            {
                "n": n,
                "title": str(hit.get("title") or url),
                "url": url,
                "favicon": None,
                "snippet": str(hit.get("snippet") or ""),
                "kind": "web",
            }
        )
    return sorted(out, key=lambda item: item["n"])


def with_web_sources(parts: list[dict[str, Any]], sources: list[dict[str, Any]]) -> None:
    """Add `sources` to the reply's `sources` part (an edition's, if it made
    one: its numbers win), or as a new one."""
    if not sources:
        return
    for part in parts:
        if part.get("type") == "sources" and isinstance(part.get("items"), list):
            taken = {item.get("n") for item in part["items"] if isinstance(item, Mapping)}
            part["items"].extend(item for item in sources if item["n"] not in taken)
            return
    parts.append({"type": "sources", "items": sources})
