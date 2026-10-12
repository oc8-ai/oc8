"""Contribute the `opaas_ai` model provider (OpenAI-compatible LiteLLM proxy).

All specifics of this provider live here in the plugin: the endpoint URL and the
adapter wiring. The key (and an optional base_url override) are resolved by the
core's unified credentials framework -- a `Credential` row of this plugin's own
`opaas_ai_api_key` type (credential_types/opaas_ai_api_key.toml), the exact same
`resolve_model_key`/`resolve_model_base_url` path a core provider's
`{canonical}_api_key` type goes through (oc8.modelrouter.keys) -- and handed to
the factory as `api_key`/`base_url`.
"""

from __future__ import annotations

from typing import Any

from oc8.modelrouter.adapters.openai_compatible import OpenAICompatibleAdapter

# The oc8 AI (powered by opaas) OpenAI-compatible base URL. A per-config base_url still wins.
DEFAULT_BASE_URL = "https://ai.opaas.online/v1"


def register(contrib: Any) -> None:
    contrib.add_model_provider(
        canonical="opaas_ai",
        locality="cloud",
        factory=lambda _settings, base_url, api_key: OpenAICompatibleAdapter(
            base_url or DEFAULT_BASE_URL, api_key or ""
        ),
        # Available once a tenant has stored its key (BYOK).
        available=lambda _settings, key: bool(key),
        aliases=("opaas", "odoo-gpt"),
    )
