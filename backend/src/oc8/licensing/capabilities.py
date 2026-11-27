"""Shared capability typing and names consumed by implemented features."""

type Capability = str

ENTERPRISE_SUPERVISION_ADVANCED: Capability = "enterprise.supervision.advanced"
ENTERPRISE_SSO_SAML: Capability = "enterprise.sso.saml"
# oc8 Chat C5 (spec §6.2): implemented in oc8-enterprise; named here so the
# core gates (edition tools, frontend slots) and governance can speak of them.
# Retired as a gate: web search is a Community core tool (`web_search`,
# oc8.websearch, spec §10 of the chat experience phase 1c) and no core code
# checks this name. It stays known only so a licence issued with it still
# reads; an edition tool can no longer be named `web_search` (core owns it).
ENTERPRISE_WEB_SEARCH: Capability = "enterprise.web_search"
ENTERPRISE_CANVAS: Capability = "enterprise.canvas"
ENTERPRISE_DATA_ANALYSIS: Capability = "enterprise.data_analysis"
ENTERPRISE_CHAT_SEARCH: Capability = "enterprise.chat_search"
ENTERPRISE_CHAT_SHARING: Capability = "enterprise.chat_sharing"
ENTERPRISE_DICTATION: Capability = "enterprise.dictation"
ENTERPRISE_PROMPT_TEMPLATES: Capability = "enterprise.prompt_templates"
ENTERPRISE_OPENAPI_IMPORT: Capability = "enterprise.openapi_import"
ENTERPRISE_EMBEDDING: Capability = "enterprise.embedding"

ALL_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        ENTERPRISE_SUPERVISION_ADVANCED,
        ENTERPRISE_SSO_SAML,
        ENTERPRISE_WEB_SEARCH,
        ENTERPRISE_CANVAS,
        ENTERPRISE_DATA_ANALYSIS,
        ENTERPRISE_CHAT_SEARCH,
        ENTERPRISE_CHAT_SHARING,
        ENTERPRISE_DICTATION,
        ENTERPRISE_PROMPT_TEMPLATES,
        ENTERPRISE_OPENAPI_IMPORT,
        ENTERPRISE_EMBEDDING,
    }
)
