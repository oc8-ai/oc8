"""Plugin manifest v2 (§13.3): generalizes the agent-module manifest with a
plugin type, trust level, permissions, capabilities, entry points and hook
bindings. Backward compatible — old agent-module manifests parse unchanged."""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

logger = logging.getLogger(__name__)

PluginType = Literal[
    "skill",
    "flow_template",
    "agent_template",
    "department_template",
    "connector",
    "model_adapter",
    "runtime_adapter",
    "tool_pack",
    "approval_channel",
    "core_extension",
]
TrustLevel = Literal["first_party", "verified", "community"]
SourceFormat = Literal["oc8", "claude", "hybrid"]

#: Permissions a Claude-format hook may require. Derived at discovery from
#: ``claude_hooks`` when not declared explicitly in ``plugin.toml``.
CLAUDE_HOOK_PERMISSIONS = {
    "command": "hooks:claude:command",
    "http": "hooks:claude:http",
    "mcp_tool": "hooks:claude:mcp",
}


class ManifestError(ValueError):
    """A manifest failed schema validation."""


class HookBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    point: str
    priority: int = 50
    replace: bool = False


class ClaudeHookAction(BaseModel):
    """One action inside a Claude hooks.json matcher (command, http, …)."""

    model_config = ConfigDict(extra="allow")
    type: str
    command: str = ""
    url: str = ""
    server: str = ""
    tool: str = ""
    prompt: str = ""
    agent: str = ""


class ClaudeHookMatcher(BaseModel):
    """Event matcher block from Claude ``hooks/hooks.json``."""

    model_config = ConfigDict(extra="allow")
    matcher: str = ""
    hooks: list[ClaudeHookAction] = []


class ClaudeHooksSpec(BaseModel):
    """Normalised Claude hooks — separate from oc8 ``handles`` entry points."""

    model_config = ConfigDict(extra="forbid")
    events: dict[str, list[ClaudeHookMatcher]] = {}
    #: Component paths Claude declared unsupported in oc8 v1 (themes, lsp, …).
    unsupported: list[str] = []


class SandboxManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image: str | None = None
    build: str | None = None
    services: list[str] = []


class TemplateAgentTrigger(BaseModel):
    """A cron schedule an agent template ships. The ONLY trigger kind a
    template may declare -- a webhook trigger's token is a secret and an
    event trigger names a source that may not exist in the target tenant,
    so neither survives an export/install round trip (design's Non-Goals)."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["cron"] = "cron"
    cron_expression: str
    task_text: str


class TemplateAgent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    role_title: str = ""
    mission: str = ""
    is_team_lead: bool = False
    reports_to: str | None = None
    skills: list[str] = []
    persona: str = ""
    narrowing: dict[str, Any] = {}
    # Per-agent step budget (0 = framework default). Lets an agent plugin give a
    # long, multi-record workflow more room without touching the core.
    max_steps: int = 0
    # Per-agent cron schedule a template ships (Capa-Exporter design,
    # Component 3). None = no trigger, the same as every pre-existing
    # manifest that predates this field.
    trigger: TemplateAgentTrigger | None = None


class DepartmentTemplateSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frame: dict[str, Any] = {}
    agents: list[TemplateAgent] = []
    starter_tasks: list[dict[str, Any]] = []  # reserved; not instantiated in v1


class SkillTemplateSpec(BaseModel):
    """A skill a plugin ships. Data only -- no code loading involved."""

    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    category: str = ""
    instruction: str = ""
    requires_tools: list[str] = []
    requires_kbs: list[str] = []
    guardrails: list[str] = []
    #: Where this skill's own `references/`/`assets/`/`scripts/` directories
    #: live, relative to the CAPA root -- "" for a skill whose SKILL.md sits at
    #: the capa root itself, "skills/<slug>" for one nested under skills/, and
    #: None for a skill with no on-disk directory at all (a `skills/*.toml`
    #: native skill, or an inline command). None is exactly the case with
    #: nothing for `read_reference_file` (agent/control_tools.py) to serve.
    reference_root: str | None = None


class SkillPackSpec(BaseModel):
    """SEVERAL skills in one plugin.

    A `skill` plugin could ship exactly one, so a standard set for a company
    meant one plugin per skill -- installed, consented to and enabled one at a
    time. That is friction with no purpose: the skills in a set are chosen
    together, and an operator wants to turn the SET on.

    `skill_template` stays for the plugins that already use it; a manifest may
    carry either, or both.
    """

    model_config = ConfigDict(extra="forbid")
    skills: list[SkillTemplateSpec] = []


class FlowTemplateSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    spec: dict[str, Any] = {}


class GuardrailPreset(BaseModel):
    """A named permission set a tool-pack plugin ships for one connection.

    Data only -- applying one just writes an ordinary policy into the
    department frame. `authz/pdp.py` never sees this model.
    """

    model_config = ConfigDict(extra="forbid")
    key: str
    label: str
    summary: str
    recommended: bool = False
    read: bool = False
    modify: bool = False
    approval_actions: list[str] = []
    approval_eur: int | None = None
    #: The ONLY tool names this preset puts within reach; empty means all of the
    #: connection's tools. Maps onto `ToolPolicy.only`, which already enforces a
    #: surface as well as a set of rights.
    #:
    #: Needed because rights are coarser than consequences. Odoo classifies
    #: `delete_record` as a `send`, and a deletion carries no monetary value --
    #: so a preset offering "autonomous, approval above EUR 1000" would let
    #: deletions through unattended, since an unreadable value counts as zero
    #: and never meets the threshold. Naming the reachable tools is how a
    #: connection-level preset can say "work freely, but not that" without
    #: needing per-tool policies.
    only: list[str] = []

    @field_validator("approval_eur", mode="before")
    @classmethod
    def _empty_string_means_no_threshold(cls, v: Any) -> Any:
        # TOML has no null; manifests use "" for "no threshold".
        if v == "":
            return None
        return v

    @field_validator("approval_actions")
    @classmethod
    def _actions_are_rights(cls, v: list[str]) -> list[str]:
        from oc8.authz.pdp import RIGHTS

        unknown = [a for a in v if a not in RIGHTS]
        if unknown:
            raise ValueError(
                f"guardrail preset approval_actions names unknown action(s): {unknown}"
            )
        return v


def parse_guardrail_preset(data: dict[str, Any]) -> GuardrailPreset:
    """Parse one `guardrails/<key>.toml` file's content (with `kind` and
    `connection` already stripped by the caller) into a `GuardrailPreset`.
    Raises `ManifestError` on any validation failure."""
    try:
        return GuardrailPreset.model_validate(data)
    except ValidationError as exc:
        raise ManifestError(str(exc)) from exc


class GuardrailAttribute(BaseModel):
    """One named, typed value a tool-pack plugin declares as technically
    extractable from a call to one of its tools -- the generalisation of
    `value_spec` (which only ever extracted one anonymous number) into a
    named catalog a "with limits" Condition (`authz/pdp.py`) can reference.

    The PDP knows nothing about order values, domains or environments; it
    only evaluates `Condition.attribute` against whatever `attributes` dict
    it is handed. This model is how a plugin says which attribute *names*
    exist for a given tool and how to pull each one out of a real call's
    arguments -- the UI's Conditions editor may only ever offer attributes
    declared here for the tool being edited.
    """

    model_config = ConfigDict(extra="forbid")
    key: str
    label: str
    datatype: Literal["number", "string", "boolean", "enum"] = "number"
    #: Only meaningful when datatype == "enum": the values a user may compare
    #: against, e.g. ["staging", "production"].
    enum_values: list[str] = []
    #: Tool names this attribute applies to. Empty means every tool on the
    #: connection may expose it (extraction simply yields nothing for a call
    #: whose arguments don't match the spec).
    tools: list[str] = []
    #: Opaque, datatype-dependent extraction spec, interpreted by
    #: `agent/tool_semantics.py:extract_attributes` -- same `direct_fields`/
    #: `line_items` shape `value_spec` already uses for `datatype == "number"`;
    #: a plain `{"field": "..."}` path lookup for string/boolean/enum.
    extract: dict[str, Any] = {}

    @model_validator(mode="after")
    def _enum_needs_values(self) -> GuardrailAttribute:
        if self.datatype == "enum" and not self.enum_values:
            raise ValueError(f"attribute {self.key!r} has datatype='enum' but no enum_values")
        return self


class ToolPackConnection(BaseModel):
    """An MCP server a tool pack describes. It is materialised DISCONNECTED --
    a manifest may describe a server, but only an operator may declare it
    reachable."""

    model_config = ConfigDict(extra="forbid")
    key: str = "default"
    name: str
    server_url: str
    transport: str = "stdio"
    # Either a flat list, or the classification dict {"read":[...], "write":[...],
    # "send":[...]} the runtime uses to map each tool to a right. A plugin that
    # needs value-gated approval on writes supplies the dict form.
    scopes: list[str] | dict[str, Any] = []
    config: dict[str, Any] = {}
    # The credential_type (from this Capa's own credential_types/*.toml, per
    # the Unified Credentials Framework) a login for this connection must be.
    # Empty for a connection that predates this design (OAuth/department-
    # scoped tool packs) -- those are untouched and never set this.
    credential_type: str = ""
    # env var name -> the credential_type's own field key, mirroring
    # PluginSetupSpec.mcp.secret_env_fields' existing convention exactly.
    credential_env_fields: dict[str, str] = {}
    #: Named permission sets this connection's plugin author recommends.
    #: Offered as a choice by the picker; free configuration stays available.
    guardrail_presets: list[GuardrailPreset] = []
    #: Named, typed attributes this connection's tools expose for "with
    #: limits" Conditions (see `GuardrailAttribute`). Empty for a connection
    #: that predates the generic condition model or has nothing to gate on.
    guardrail_attributes: list[GuardrailAttribute] = []

    @model_validator(mode="after")
    def _validate_guardrail_presets(self) -> ToolPackConnection:
        keys = [p.key for p in self.guardrail_presets]
        seen: set[str] = set()
        dupes: set[str] = set()
        for k in keys:
            if k in seen:
                dupes.add(k)
            seen.add(k)
        if dupes:
            names = ", ".join(sorted(dupes))
            msg = f"connection '{self.key}' has duplicate guardrail preset key(s): {names}"
            raise ValueError(msg)
        attr_keys = [a.key for a in self.guardrail_attributes]
        attr_dupes = {k for k in attr_keys if attr_keys.count(k) > 1}
        if attr_dupes:
            names = ", ".join(sorted(attr_dupes))
            msg = f"connection '{self.key}' has duplicate guardrail attribute key(s): {names}"
            raise ValueError(msg)
        recommended = [p.key for p in self.guardrail_presets if p.recommended]
        if len(recommended) > 1:
            raise ValueError(
                f"connection '{self.key}' has more than one recommended guardrail preset: "
                f"{', '.join(recommended)}"
            )
        has_value_spec = "value_spec" in self.config
        if not has_value_spec:
            for preset in self.guardrail_presets:
                if preset.approval_eur is not None:
                    logger.warning(
                        "guardrail preset '%s' on connection '%s' sets approval_eur but the "
                        "connection has no value_spec; dropping the threshold",
                        preset.key,
                        self.key,
                    )
                    preset.approval_eur = None
        return self


class ToolPackSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connections: list[ToolPackConnection] = []


class SetupFieldSpec(BaseModel):
    """One input rendered by the generic plugin setup surface."""

    model_config = ConfigDict(extra="forbid")
    key: str
    label: str
    kind: Literal["text", "url", "password", "department", "credential", "select"] = "text"
    required: bool = True
    default: str = ""
    placeholder: str = ""
    help: str = ""
    # Where a `password`-kind value is stored. Empty means the default
    # `plugin:{manifest.name}:{key}` convention (what an MCP tool pack uses);
    # a plugin whose OWN code already reads a fixed name -- an approval
    # channel's `[plugin.config].secret_ref`, resolved by channels/registry.py
    # -- names it here instead, so the wizard writes to where the plugin reads.
    secret_ref: str = ""
    # Only meaningful when kind == "credential": names the credential_type
    # this field must resolve to (e.g. "s3_api", "telegram_bot"). Renders as
    # <CredentialPicker credentialType={...}> instead of a raw input --
    # configure_plugin then treats the submitted value as a credential id,
    # not a plaintext value (unified credentials framework design, §7).
    credential_type: str = ""
    # Only meaningful when kind == "select": the fixed choices rendered as a
    # <select>, in order. The submitted value is one of these verbatim --
    # there is no separate value/label pair, so a plugin that wants a
    # human-friendly label puts it in `help` rather than here.
    options: list[str] = []

    @model_validator(mode="after")
    def _credential_kind_needs_a_type(self) -> SetupFieldSpec:
        if self.kind == "credential" and not self.credential_type:
            raise ValueError(
                f"field {self.key!r} has kind='credential' but no credential_type -- "
                "name which credential_types entry it must resolve to"
            )
        if self.kind != "credential" and self.credential_type:
            raise ValueError(
                f"field {self.key!r} sets credential_type but kind is {self.kind!r}, "
                "not 'credential' -- credential_type is only read for kind='credential'"
            )
        return self

    @model_validator(mode="after")
    def _select_kind_needs_options(self) -> SetupFieldSpec:
        if self.kind == "select" and not self.options:
            raise ValueError(
                f"field {self.key!r} has kind='select' but no options -- "
                "name the choices it must render"
            )
        if self.kind != "select" and self.options:
            raise ValueError(
                f"field {self.key!r} sets options but kind is {self.kind!r}, "
                "not 'select' -- options is only read for kind='select'"
            )
        if self.kind == "select" and self.default and self.default not in self.options:
            raise ValueError(
                f"field {self.key!r} defaults to {self.default!r}, which is not one of "
                f"its own options {self.options!r}"
            )
        return self


class SetupMcpSpec(BaseModel):
    """Declarative mapping from setup fields into one MCP connection."""

    model_config = ConfigDict(extra="forbid")
    connection_key: str = "default"
    name: str
    # Empty for a remote-MCP or manual-HTTP connection -- `configure_plugin`
    # (api/v1/capas.py) still writes this verbatim into the connection's
    # `config["command"]`, but nothing reads that key for a non-stdio
    # transport (see agent/mcp_client.py's `open_tool_session`).
    command: str = ""
    args: list[str] = []
    scopes: list[str] = []
    env: dict[str, str] = {}
    env_fields: dict[str, str] = {}
    secret_env_fields: dict[str, str] = {}
    department_field: str | None = None


class SetupOAuthProvision(BaseModel):
    """Declares that submitting this plugin's setup form must also provision an
    OAuthConnection, through core's existing provider registry rather than
    vendor logic baked into the setup endpoint.

    `provider` names an entry in `oc8.oauth.provisioning.PROVISIONERS`, which is
    keyed exactly like `oc8.oauth.providers` -- so a manifest can ask for a
    connection core already knows how to mint, and nothing more.

    `connector_type` + `site_ids_field` are the optional second half: when the
    form also collects a comma-separated list of source ids, a DataSource of
    that connector_type is created against the same OAuth connection, so the
    knowledge side of a combined tool-pack/connector plugin does not need a
    second form.
    """

    model_config = ConfigDict(extra="forbid")
    provider: str
    connector_type: str = ""
    site_ids_field: str = ""
    # The DataSource config key `_provision_from_setup` writes the parsed
    # site_ids_field values under. Defaults to "siteIds" (Microsoft 365's own
    # connector config key, unaffected). google_workspace's connector
    # requires "sharedDriveIds" instead -- without this field,
    # `_provision_from_setup` would hardcode "siteIds" regardless of what the
    # connector actually expects, and every Shared-Drive-naming setup
    # submission would 422 with "knowledge source rejected" (the connector's
    # own validate() sees no `sharedDriveIds` key at all).
    source_ids_config_key: str = "siteIds"
    # Optional second id list, independent of site_ids_field -- microsoft365's
    # connector indexes SharePoint sites AND OneDrive drives in one DataSource,
    # under two separate config keys (`siteIds`/`driveIds`), so the form needs
    # two fields rather than one. Always written under the connector's own
    # "driveIds" key -- unlike source_ids_config_key there is no per-connector
    # override, since only microsoft365 declares this field at all. Left empty
    # (the default), `_provision_from_setup` behaves exactly as before this
    # existed: single id list, single config key.
    drive_ids_field: str = ""
    # Domain-wide-delegation-style setup: a comma-separated setup field naming
    # one or more identities this connection should mint a SEPARATE, per-identity
    # delegated token for (agent/mcp_env.py's "oauth-delegated:" convention,
    # resolved fresh per MCP-bridge launch, cached per (connection, subject) --
    # unlike the single self-identity "oauth:" token every plugin can already
    # use). Left empty, this whole mechanism is a no-op -- a plugin with a
    # single, non-delegating identity (Microsoft 365's client_credentials app,
    # for instance) never sets these fields.
    delegated_identities_field: str = ""
    delegated_identities_env: str = ""
    delegated_token_env_prefix: str = ""
    # The OAuth scope string `mint_delegated_token` is called with for every
    # identity this connection delegates to -- manifest-declared, never a
    # Python default baked into `oauth/tokens.py` or `mcp_env.py`, so a future
    # plugin needing delegated identities for a different scope set never has
    # to edit shared core code to get its own scope.
    delegated_scope: str = ""


class SetupValidationSpec(BaseModel):
    """Generic cross-field validation supplied by a plugin manifest.

    Each ``any_of`` alternative lists fields that must all be present. This
    covers common credential alternatives without teaching OC8 about a vendor.
    """

    model_config = ConfigDict(extra="forbid")
    any_of: list[list[str]] = []


class CredentialTypeSpec(BaseModel):
    """A reusable, named credential SHAPE a Capa (or core) declares -- the
    unified credentials framework's equivalent of n8n's `ICredentialType`.

    One of these, assembled from a `credential_types/<name>.toml` file
    (design §2), describes what a `Credential` row of this `credential_type`
    holds: which fields, which of them are secret. `validate_entry_point`
    (same convention as `PluginSetupSpec`'s) proves a submitted credential
    actually works.
    """

    model_config = ConfigDict(extra="forbid")
    name: str
    display_name: str
    fields: list[SetupFieldSpec] = []
    validate_entry_point: str = ""

    @field_validator("fields")
    @classmethod
    def _no_field_is_itself_a_credential(cls, v: list[SetupFieldSpec]) -> list[SetupFieldSpec]:
        # A credential type's OWN fields describe what IT stores (region,
        # access_key, ...) -- none of them can in turn point at ANOTHER
        # credential_type. That indirection has no defined resolution order
        # and no consumer needs it; reject it at parse time rather than let
        # it silently do nothing at test/resolve time.
        nested = [f.key for f in v if f.kind == "credential"]
        if nested:
            raise ValueError(
                f"credential_types field(s) {nested} have kind='credential' -- "
                "a credential type's own fields may not reference another credential type"
            )
        return v


class PersonalSettingsSpec(BaseModel):
    """A capa's own contributed row for a setting a PERSON manages for
    themselves -- distinct from `setup` above (an admin's one-time,
    tenant-wide configuration) the same way a `credential_type` is distinct
    from a plain setup field: this is the generalizable slot, `setup` is the
    admin-only one.

    `location` names WHERE this gets rendered -- a stable key the location
    itself owns and interprets, not a route core validates. Today exactly one
    location listens: the Profile page's "Approval channels" panel
    (`frontend/src/routes/profile.tsx`), keyed to `"approval_channels"`. A
    capa author is free to name a new location string; it simply renders
    nowhere until something claims it, the same non-failure a `surfaces` entry
    with no matching UI tab already tolerates.

    This proves the pattern with ONE real consumer (telegram_approvals,
    whatsapp_approvals: linking a person's own chat) rather than building a
    generic settings-panel plugin SDK nothing uses yet.
    """

    model_config = ConfigDict(extra="forbid")
    label: str
    location: str = "approval_channels"


class PluginSetupSpec(BaseModel):
    """A data-only setup contract interpreted by OC8's generic renderer.

    `mcp` is optional: a tool_pack plugin sets it up because a value here BECOMES
    an McpConnection. An approval_channel plugin has no connection to create --
    it just needs a credential resolved by channels/registry.py -- so its setup
    ends at "values stored", not "connection materialised". Whether a
    `setup_validate` entry point exists (checked against the SAME manifest, not
    declared here) decides whether "stored" was also proven to work.
    """

    model_config = ConfigDict(extra="forbid")
    title: str
    description: str = ""
    submit_label: str = "Save & test"
    fields: list[SetupFieldSpec] = []
    validation: SetupValidationSpec = SetupValidationSpec()
    mcp: SetupMcpSpec | None = None
    # Optional, like `mcp` above: only a plugin whose credentials ARE an OAuth
    # app registration (Microsoft 365's client_credentials app) declares this.
    oauth_provision: SetupOAuthProvision | None = None
    # `module:attr`, called with the submitted values once they pass generic
    # validation but before anything is committed. Deliberately NOT one of
    # `entry_points` above: those are all "import me once at enable time and
    # call register(contrib)" -- a different signature and a different moment
    # -- and load_plugin iterates that whole dict, so anything placed there
    # gets called as register(contrib) too, silently and wrongly.
    validate_entry_point: str = ""

    @model_validator(mode="after")
    def _oauth_provision_needs_mcp(self) -> PluginSetupSpec:
        """`oauth_provision` without `mcp` would be silently ignored.

        `configure_plugin` returns early into `_configure_without_connection`
        when `setup.mcp is None`, and the provisioning block sits after that
        return, inside the MCP-connection branch. A connector-only manifest
        declaring `oauth_provision` alone would therefore get HTTP 200, no
        OAuthConnection, no DataSource and no error at all. Rejecting the
        manifest at parse time says so at the moment it is fixable, rather than
        restructuring the endpoint around a combination nothing ships yet.
        """
        if self.oauth_provision is not None and self.mcp is None:
            raise ValueError(
                "[plugin.setup.oauth_provision] requires [plugin.setup.mcp]: "
                "provisioning runs on the MCP-connection path of the setup "
                "endpoint, so declaring it without `mcp` would be ignored"
            )
        return self


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    name: str
    version: str
    type: PluginType = "agent_template"
    core_compat: str = ""
    trust: TrustLevel = "first_party"
    # Human-readable one-liner shown in the "available plugins" UI.
    summary: str = ""
    # Optional display label for runtime picker UIs (e.g., "nanoclaw" instead of "nanoclaw_runtime")
    label: str = ""
    # A filename (or relative path -- no `..` segments) to an image file
    # inside this plugin's OWN folder, e.g. "icon.svg", served by
    # GET /plugins/{plugin_id}/icon for the Capas UI. None (the default)
    # means the UI falls back to a generic icon for `type`.
    icon: str | None = None
    permissions: list[str] = []
    capabilities: list[str] = []
    provides: list[str] = []
    entry_points: dict[str, str] = {}
    handles: list[HookBinding] = []
    # retained agent-module fields (backward compatible)
    depends: list[str] = []
    # Plugin *names* (folder name = manifest name) that must be installed
    # before this plugin can be, auto-resolved recursively by
    # `install_from_disk` (api/v1/capas.py) -- NOT the same thing as
    # `depends` above, which names abstract capability strings a tenant's
    # install must already satisfy and is never auto-resolved. Presence-only,
    # no version constraint syntax (design §9).
    plugin_depends: list[str] = []
    # PEP 508 requirement strings this plugin's IN-PROCESS code needs already
    # importable in core's own environment. Checked by `loader.py` immediately
    # before import (Task 10) -- refuses to load with a clear, specific error
    # naming exactly what is missing, rather than a raw ModuleNotFoundError
    # deep in an import traceback (design §10.2). A plugin whose ONLY code is
    # an MCP subprocess bridge declares its requirements in `tool_pack.toml`
    # instead (design §10.1) -- these two lists are never the same list.
    requirements: list[str] = []
    model: dict[str, Any] = {}
    sandbox: SandboxManifest | None = None
    # Single-agent hire template. Optional for backward compatibility with
    # legacy manifests that defaulted to type=agent_template with no body;
    # new capas should always set it -- instantiate_agent reads it.
    agent_template: TemplateAgent | None = None
    department_template: DepartmentTemplateSpec | None = None
    skill_template: SkillTemplateSpec | None = None
    skill_pack: SkillPackSpec | None = None
    flow_template: FlowTemplateSpec | None = None
    tool_pack: ToolPackSpec | None = None
    setup: PluginSetupSpec | None = None
    # Credential SHAPES this Capa contributes (design §2) -- distinct from
    # `setup` above, which is one Capa's own per-installation configuration
    # form. A credential_type is named, reusable across every Capa/source
    # that declares a field of kind="credential" with this name -- never
    # scoped to just this one Capa's own setup.
    credential_types: list[CredentialTypeSpec] = []
    # Optional -- a capa's own contributed row for a PERSONAL setting (see
    # PersonalSettingsSpec's own docstring for what distinguishes this from
    # `setup` above).
    personal_settings: PersonalSettingsSpec | None = None
    # Plain settings a code-carrying plugin needs to build itself: an approval
    # channel's classification ceiling, the NAME of the secret holding its
    # credential. Never the credential -- a manifest is read from disk by
    # anybody who can list the plugins folder, and `setup` above is the path for
    # values an operator supplies per tenant.
    config: dict[str, Any] = {}
    tools: list[str] = []
    mcp: list[dict[str, Any]] = []
    skills: list[str] = []
    triggers: list[str] = []
    policy: dict[str, Any] = {}
    io: dict[str, Any] = {}
    phases: list[dict[str, Any]] = []
    hooks: dict[str, Any] = {}
    #: Claude Agent SDK ``hooks/hooks.json`` (event-based), bridged at runtime.
    claude_hooks: ClaudeHooksSpec | None = None
    #: How this capa was assembled on disk — metadata for UI and debugging.
    source_format: SourceFormat = "oc8"


def claude_hook_permissions(spec: ClaudeHooksSpec | None) -> list[str]:
    """Derive consent permissions from declared Claude hook action types."""
    if spec is None:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for matchers in spec.events.values():
        for matcher in matchers:
            for action in matcher.hooks:
                perm = CLAUDE_HOOK_PERMISSIONS.get(action.type)
                if perm and perm not in seen:
                    seen.add(perm)
                    out.append(perm)
    return out


def parse_manifest(data: dict[str, Any]) -> Manifest:
    try:
        return Manifest.model_validate(data)
    except ValidationError as exc:
        raise ManifestError(str(exc)) from exc
