"""Core-owned credential types -- provider completion keys (unified
credentials framework, design §8). Registered at import time, mirroring
how a Capa's own `credential_types` are read from its manifest, except
these have no owning Capa: Settings > Models is a core feature.

The provider set mirrors `oc8.modelrouter.registry._PROVIDERS` exactly --
the real source of truth `models.tsx`'s own `ProviderGroup`/`group.canonical`
values are drawn from via `useModelProviders()` (`GET /models/providers`,
backed by `available_provider_entries`/`provider_infos`). `ollama` is
deliberately excluded: it is `locality="local"` and has no key concept at
all (`ProviderCard` never renders a completion-key affordance for it,
`resolve_model_key` always returns `None` for it). Any OTHER provider a
tenant can configure comes from an enabled Capa's own `model_adapter`
contribution and declares its own `credential_types` via its manifest --
never registered here.
"""

from __future__ import annotations

from oc8.capas.manifest import CredentialTypeSpec, SetupFieldSpec
from oc8.credentials.registry import CORE_CREDENTIAL_TYPES


def _provider_key_type(
    canonical: str, display_name: str, *, extra_fields: list[SetupFieldSpec] | None = None
) -> CredentialTypeSpec:
    return CredentialTypeSpec(
        name=f"{canonical}_api_key",
        display_name=display_name,
        fields=[
            SetupFieldSpec(key="api_key", label="API key", kind="password"),
            *(extra_fields or []),
        ],
    )


CORE_CREDENTIAL_TYPES["anthropic_api_key"] = _provider_key_type("anthropic", "Anthropic")
CORE_CREDENTIAL_TYPES["openai_api_key"] = _provider_key_type("openai", "OpenAI")
CORE_CREDENTIAL_TYPES["openai_compatible_api_key"] = _provider_key_type(
    "openai_compatible",
    "OpenAI-compatible",
    # Unlike anthropic/openai (fixed endpoints), an "openai_compatible"
    # provider IS its base_url -- a self-hosted LiteLLM/vLLM gateway, a
    # third-party proxy (e.g. opaas.cloud). There is no sane default,
    # mirroring n8n's OpenAiApi credential's own `url` field (there
    # defaulted to the real OpenAI endpoint since that credential type
    # doubles as "OpenAI or an OpenAI-shaped proxy"; oc8 splits the two
    # into separate provider types, so this one has nothing to default to).
    extra_fields=[
        SetupFieldSpec(
            key="base_url",
            label="Server URL",
            kind="url",
            placeholder="https://api.example.com/v1",
            help=(
                "The OpenAI-compatible endpoint's base URL, including any "
                "/v1 path segment it needs."
            ),
        )
    ],
)

# ChatGPT subscription auth (design: docs/superpowers/specs/2026-08-26-
# chatgpt-subscription-auth-design.md). Deliberately NOT built from
# _provider_key_type -- this credential holds no user-typed secret field
# at all. Its one field is a pointer to the OAuthConnection the device-
# code login created (see oc8.api.v1.catalog.create_chatgpt_subscription_
# credential); resolve_model_key's own branch (oc8.modelrouter.keys)
# reads that pointer and calls oc8.oauth.tokens.get_access_token()
# instead of the normal secret-store field lookup every other credential
# type here uses.
# A registry login any runtime capa can ask for in its setup. The password
# stays in the secret store. A pull reads it once and does not copy it into
# the container.
CORE_CREDENTIAL_TYPES["container_registry"] = CredentialTypeSpec(
    name="container_registry",
    display_name="Container registry",
    fields=[
        SetupFieldSpec(
            key="registry",
            label="Registry host",
            kind="text",
            placeholder="ghcr.io",
            help="Host only, such as ghcr.io. No scheme and no repository path.",
        ),
        SetupFieldSpec(key="username", label="Username", kind="text"),
        SetupFieldSpec(key="password", label="Password or token", kind="password"),
    ],
)

CORE_CREDENTIAL_TYPES["openai_chatgpt_subscription"] = CredentialTypeSpec(
    name="openai_chatgpt_subscription",
    display_name="ChatGPT subscription",
    fields=[],
)

# SMTP server (design: docs/superpowers/specs/2026-08-28-account-self-
# service-design.md §4.1). Reuses the credentials framework unchanged for
# a system-level integration, not a per-agent one -- nothing else in this
# codebase ever asks for a "smtp_server"-typed credential, so it simply
# never appears in any per-agent tool/model credential picker.
CORE_CREDENTIAL_TYPES["smtp_server"] = CredentialTypeSpec(
    name="smtp_server",
    display_name="SMTP server",
    fields=[
        SetupFieldSpec(key="host", label="Host", kind="text"),
        SetupFieldSpec(key="port", label="Port", kind="text", default="587"),
        # Optional on purpose: some internal/relay SMTP servers accept
        # anonymous connections. `validate_smtp` only attempts AUTH when a
        # username is present.
        SetupFieldSpec(key="username", label="Username", kind="text", required=False),
        SetupFieldSpec(key="password", label="Password", kind="password", required=False),
        SetupFieldSpec(
            key="from_address",
            label="From address",
            kind="text",
            placeholder="noreply@yourcompany.com",
        ),
        SetupFieldSpec(
            key="use_tls",
            label="Use STARTTLS",
            kind="text",
            default="true",
            help='"true" or "false".',
        ),
    ],
    validate_entry_point="oc8.credentials.smtp:validate_smtp",
)
