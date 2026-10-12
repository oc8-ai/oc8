"""Provider registry — which adapter serves which provider.

Adding a BUILT-IN provider = its adapter file + one ProviderEntry below. A
`model_adapter` PLUGIN contributes one instead, and is resolvable only by a
tenant that installed and enabled that plugin (``resolve_provider``).

The synchronous helpers (`canonical_provider`, `build_adapter`) see plugin
providers too, because the completion path has no db session. That is not a
hole: reaching them requires a ModelConfig row naming the provider, and the
write endpoint validates that name against the tenant-scoped list. The gate is
at configuration time, the same shape as `resolve_runtime` — the tenant check
happens first, then the implementation lookup is a plain map."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from oc8.config import Settings
from oc8.modelrouter.adapters.anthropic import AnthropicAdapter
from oc8.modelrouter.adapters.chatgpt_subscription import ChatGptSubscriptionAdapter
from oc8.modelrouter.adapters.ollama import OllamaAdapter
from oc8.modelrouter.adapters.openai import OpenAIAdapter
from oc8.modelrouter.adapters.openai_compatible import OpenAICompatibleAdapter
from oc8.modelrouter.types import ModelAdapter
from oc8.oauth.openai_chatgpt_params import CHATGPT_BACKEND_BASE_URL


class ProviderInfo(TypedDict):
    canonical: str
    locality: str


@dataclass(frozen=True)
class ProviderEntry:
    canonical: str
    aliases: tuple[str, ...]
    locality: str
    factory: Callable[[Settings, str | None, str | None], ModelAdapter]
    available: Callable[[Settings, str | None], bool]
    #: Always `available` because there is nothing to check up front (a local
    #: server, a per-connection login), so only a tenant key or a model set up
    #: against it means the tenant actually uses it.
    always_available: bool = False


_PROVIDERS: tuple[ProviderEntry, ...] = (
    ProviderEntry(
        "anthropic",
        ("claude",),
        "cloud",
        lambda s, _b, key: AnthropicAdapter(key or s.anthropic_api_key),
        lambda s, key: bool(key or s.anthropic_api_key),
    ),
    ProviderEntry(
        "openai",
        ("gpt",),
        "cloud",
        lambda s, _b, key: OpenAIAdapter(key or s.openai_api_key),
        lambda s, key: bool(key or s.openai_api_key),
    ),
    ProviderEntry(
        "openai_compatible",
        ("mistral",),
        "cloud",
        lambda s, b, key: OpenAICompatibleAdapter(
            b or s.mistral_base_url, key or s.mistral_api_key
        ),
        lambda s, key: bool(key or s.mistral_api_key),
    ),
    ProviderEntry(
        "openai_chatgpt",
        (),
        "cloud",
        lambda _s, b, key: ChatGptSubscriptionAdapter(
            base_url=b or CHATGPT_BACKEND_BASE_URL, api_key=key or ""
        ),
        # Unlike the other cloud providers there is no environment-wide key to
        # check: this one is authorised per connection by a ChatGPT login, so
        # it is offered to every tenant and the login decides whether it works.
        lambda _s, _key: True,
        always_available=True,
    ),
    ProviderEntry(
        "ollama",
        ("llama", "local"),
        "local",
        lambda s, _b, _key: OllamaAdapter(s.ollama_base_url),
        lambda s, _key: True,
        always_available=True,
    ),
)

_ALIAS_TO_CANONICAL: dict[str, str] = {
    alias: e.canonical for e in _PROVIDERS for alias in (e.canonical, *e.aliases)
}
_BY_CANONICAL: dict[str, ProviderEntry] = {e.canonical: e for e in _PROVIDERS}


def _plugin_entries() -> dict[str, ProviderEntry]:
    """Providers contributed by loaded plugins, keyed by canonical name.

    Process-wide by nature (a module import is). Entitlement is decided by
    `resolve_provider` / `available_provider_entries`, never here.
    """
    from oc8.capas.contributions import providers_for

    out: dict[str, ProviderEntry] = {}
    for plugin_id in _loaded_plugin_ids():
        out.update(providers_for(plugin_id))
    return out


def _loaded_plugin_ids() -> list[str]:
    from oc8.capas.discovery import discover_plugins
    from oc8.capas.loader import load_plugin

    ids: list[str] = []
    for discovered in discover_plugins():
        if discovered.type == "model_adapter" and load_plugin(discovered):
            ids.append(discovered.plugin_id)
    return ids


def canonical_provider(name: str) -> str | None:
    """Alias or canonical name (case-insensitive) -> canonical; None if unknown."""
    canonical = _ALIAS_TO_CANONICAL.get(name.lower())
    if canonical is not None:
        return canonical
    lowered = name.lower()
    for entry in _plugin_entries().values():
        if lowered in {entry.canonical.lower(), *(a.lower() for a in entry.aliases)}:
            return entry.canonical
    return None


def build_adapter(
    canonical: str, settings: Settings, base_url: str | None = None, api_key: str | None = None
) -> ModelAdapter:
    entry = _BY_CANONICAL.get(canonical) or _plugin_entries().get(canonical)
    if entry is None:
        raise KeyError(canonical)
    return entry.factory(settings, base_url, api_key)


def known_providers() -> list[ProviderInfo]:
    return [{"canonical": e.canonical, "locality": e.locality} for e in _PROVIDERS]


class ProviderAvailability(TypedDict):
    canonical: str
    locality: str
    available: bool


def provider_infos(
    settings: Settings,
    tenant_keys: dict[str, str] | None = None,
    entries: list[ProviderEntry] | None = None,
) -> list[ProviderAvailability]:
    """`entries` defaults to the built-ins. Pass a tenant-scoped list from
    `available_provider_entries` to include that tenant's plugin providers."""
    keys = tenant_keys or {}
    source = entries if entries is not None else list(_PROVIDERS)
    return [
        {
            "canonical": e.canonical,
            "locality": e.locality,
            "available": e.available(settings, keys.get(e.canonical)),
        }
        for e in source
    ]


async def _enabled_plugin_names(db: AsyncSession, tenant_id: uuid.UUID) -> set[str]:
    from oc8.models import Capa, CapaInstallation

    rows = (
        await db.execute(
            select(Capa.name)
            .join(CapaInstallation, CapaInstallation.capa_id == Capa.id)
            .where(
                Capa.tenant_id == tenant_id,
                CapaInstallation.tenant_id == tenant_id,
                CapaInstallation.status == "enabled",
            )
        )
    ).scalars()
    return set(rows)


async def available_provider_entries(
    db: AsyncSession, *, tenant_id: uuid.UUID
) -> list[ProviderEntry]:
    """Built-in providers plus those contributed by THIS tenant's enabled
    plugins. The only list a tenant may configure a model against."""
    from oc8.capas.contributions import providers_for
    from oc8.capas.discovery import find_plugin
    from oc8.capas.loader import load_plugin

    out = list(_PROVIDERS)
    for name in await _enabled_plugin_names(db, tenant_id):
        discovered = find_plugin(name)
        if discovered is None or not load_plugin(discovered):
            continue
        out.extend(providers_for(name).values())
    return out


async def resolve_provider(
    db: AsyncSession, *, tenant_id: uuid.UUID, name: str
) -> ProviderEntry | None:
    """Tenant-scoped provider lookup by canonical name or alias. None means the
    tenant may not use it -- unknown and not-entitled are the same answer."""
    lowered = name.lower()
    for entry in await available_provider_entries(db, tenant_id=tenant_id):
        if lowered in {entry.canonical.lower(), *(a.lower() for a in entry.aliases)}:
            return entry
    return None
