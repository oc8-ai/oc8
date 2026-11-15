"""On-disk plugin discovery (§13).

An author drops a folder containing a `plugin.toml` into one of the configured
plugin roots; this module finds it, parses the manifest, and reports it. That is
ALL it does -- discovery never imports a plugin's Python code (that is the
code-loading slice) and never installs anything. The folder name is the plugin
id and must equal the manifest `name`, mirroring Odoo's addons convention so a
mismatched folder can't silently install the wrong plugin.
"""

from __future__ import annotations

import logging
import time
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from oc8.capas.claude_adapter import (
    adapt_claude_plugin,
    is_claude_plugin,
    merge_claude_and_oc8,
)
from oc8.capas.guardrails import Guardrail, GuardrailLibrary, parse_guardrail_entry
from oc8.capas.manifest import (
    DEFERRABLE_PERMISSIONS,
    CredentialTypeSpec,
    GuardrailPreset,
    ManifestError,
    PluginSetupSpec,
    SkillTemplateSpec,
    ToolPackConnection,
    parse_guardrail_preset,
    parse_manifest,
)
from oc8.config import get_settings

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "plugin.toml"
GUARDRAILS_DIRNAME = "guardrails"
SKILLS_DIRNAME = "skills"
SETUP_DIRNAME = "setup"
CREDENTIAL_TYPES_DIRNAME = "credential_types"
TOOL_PACK_FILENAME = "tool_pack.toml"
I18N_DIRNAME = "i18n"
_ROOT_TABLE = "plugin"
#: The ONLY filenames `_read_setup_folder` reads. Anything else in `setup/` is
#: an error, not a skip -- see that function's docstring.
SETUP_FILENAMES = frozenset({"fields.toml", "validation.toml", "oauth_provision.toml", "mcp.toml"})
#: The only top-level keys `fields.toml` may carry.
FIELDS_TOML_KEYS = frozenset(
    {"title", "description", "submit_label", "fields", "validate_entry_point"}
)


@dataclass(frozen=True)
class DiscoveredPlugin:
    """A plugin folder found on disk. `valid=False` means the manifest could not
    be parsed; `error` then says why and the plugin cannot be installed."""

    plugin_id: str
    path: str
    manifest: dict[str, Any] | None
    name: str
    version: str
    type: str
    trust: str
    summary: str
    valid: bool
    error: str | None
    #: The plugin's optional `guardrails/*.toml` library, assembled from its
    #: `kind = "library"` entries. `None` when the plugin has no such folder,
    #: or the folder has no library entries -- the common case, and no
    #: different from today's behaviour (design §3.1/§5.1).
    guardrail_library: GuardrailLibrary | None = None
    #: `{locale: {msgid: msgstr}}` loaded from `<folder>/i18n/*.po`. `{}` when
    #: the plugin has no `i18n/` folder -- the common case (design §4).
    #: SCAFFOLDING: nothing reads this field yet, so no `msgstr` is substituted
    #: anywhere -- a resolver belongs at the presentation layer (where
    #: `api/v1/mcp.py` builds the browser's DTOs) and has not been written.
    i18n: dict[str, dict[str, str]] = field(default_factory=dict)
    #: Non-fatal notes from Claude adapter (alien tools, unsupported components).
    warnings: list[str] = field(default_factory=list)


def _invalid(plugin_id: str, path: Path, error: str) -> DiscoveredPlugin:
    return DiscoveredPlugin(
        plugin_id=plugin_id,
        path=str(path),
        manifest=None,
        name=plugin_id,
        version="",
        type="",
        trust="",
        summary="",
        valid=False,
        error=error,
    )


def _read_guardrails_folder(
    folder: Path,
) -> tuple[dict[str | None, list[GuardrailPreset]], GuardrailLibrary | None]:
    """Read `<folder>/guardrails/*.toml`. Each file is either a `kind =
    "preset"` (merged into the named connection's guardrail_presets) or a
    `kind = "library"` entry (merged into the plugin's guardrail_library) --
    see design §3.

    Returns `({}, None)` when the folder does not exist, and `None` for the
    library whenever no `kind = "library"` file was found -- matching
    `parse_guardrails_toml`'s own `GuardrailLibrary | None` contract exactly.
    An empty `GuardrailLibrary` would NOT be equivalent: `api/v1/mcp.py`
    builds `McpConnectionDTO.guardrail_library` only when this is not None,
    so an always-present empty object flips that field from `null` to `[]`
    for every connection whose plugin ships no library.

    A preset file that omits `connection` is keyed under `None` -- "bind me
    to this plugin's sole connection". Resolving that needs the connection
    list, which only `_read` has, so it is resolved there, not here.
    """
    guardrails_dir = folder / GUARDRAILS_DIRNAME
    presets_by_connection: dict[str | None, list[GuardrailPreset]] = {}
    library_entries: list[Guardrail] = []
    if not guardrails_dir.is_dir():
        return presets_by_connection, None

    # Rejected, not skipped: `read_only.tml` (one missing letter) matches no
    # glob, so the guardrail would simply not exist -- and guardrails are the
    # permission ceiling for an agent, so losing one silently is the worst
    # possible shape for this mistake.
    not_toml = sorted(
        p.name
        for p in guardrails_dir.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.suffix != ".toml"
    )
    if not_toml:
        raise ManifestError(
            f"{guardrails_dir}: unexpected file(s) {not_toml} -- every file in guardrails/ "
            'must be a <key>.toml carrying kind = "preset" or kind = "library" '
            "(dotfiles like .DS_Store are silently ignored)"
        )

    for path in sorted(guardrails_dir.glob("*.toml")):
        expected_key = path.stem
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ManifestError(f"cannot read {path}: {exc}") from exc

        kind = raw.pop("kind", None)
        if kind not in ("preset", "library"):
            raise ManifestError(f'{path}: \'kind\' must be "preset" or "library", got {kind!r}')

        actual_key = raw.get("key")
        if actual_key != expected_key:
            raise ManifestError(
                f"{path}: filename {expected_key!r} does not match declared key {actual_key!r}"
            )

        if kind == "preset":
            # `None` means "no connection named" -> bind to the plugin's sole
            # connection in `_read`. Deliberately NOT the literal "primary":
            # ToolPackConnection.key's own default is "default".
            connection = raw.pop("connection", None)
            preset = parse_guardrail_preset(raw)
            presets_by_connection.setdefault(connection, []).append(preset)
        else:
            library_entries.append(parse_guardrail_entry(raw))

    if not library_entries:
        return presets_by_connection, None
    try:
        library = GuardrailLibrary(guardrail=library_entries)
    except ValidationError as exc:
        raise ManifestError(f"{guardrails_dir}: {exc}") from exc
    return presets_by_connection, library


def _read_skills_folder(folder: Path) -> list[SkillTemplateSpec]:
    """Read `<folder>/skills/*.toml` and `skills/<slug>/SKILL.md`.

    Native oc8 capas use ``*.toml``; Claude Agent SDK plugins use nested
    ``SKILL.md`` directories. Both may coexist in a hybrid capa.
    """
    skills_dir = folder / SKILLS_DIRNAME
    if not skills_dir.is_dir():
        return []

    unexpected = sorted(
        p.name
        for p in skills_dir.iterdir()
        if p.is_file()
        and not p.name.startswith(".")
        and p.suffix != ".toml"
    )
    unexpected_dirs = sorted(
        p.name
        for p in skills_dir.iterdir()
        if p.is_dir() and not (p / "SKILL.md").is_file()
    )
    if unexpected or unexpected_dirs:
        detail = []
        if unexpected:
            detail.append(f"unexpected file(s) {unexpected}")
        if unexpected_dirs:
            detail.append(
                f"unexpected director(ies) {unexpected_dirs} -- each subfolder needs SKILL.md "
                "or use skills/<slug>.toml at the top level"
            )
        raise ManifestError(f"{skills_dir}: {'; '.join(detail)}")

    specs: list[SkillTemplateSpec] = []
    seen: set[str] = set()

    for path in sorted(skills_dir.glob("*.toml")):
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ManifestError(f"cannot read {path}: {exc}") from exc
        try:
            spec = SkillTemplateSpec.model_validate(raw)
        except ValidationError as exc:
            raise ManifestError(f"{path}: {exc}") from exc
        if spec.name in seen:
            raise ManifestError(f"{skills_dir}: duplicate skill name {spec.name!r}")
        seen.add(spec.name)
        specs.append(spec)

    from oc8.capas.claude_adapter import skill_from_md

    for sub in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        skill_md = sub / "SKILL.md"
        if skill_md.is_file():
            spec = skill_from_md(skill_md, reference_root=str(sub.relative_to(folder)))
            if spec.name in seen:
                raise ManifestError(f"{skills_dir}: duplicate skill name {spec.name!r}")
            seen.add(spec.name)
            specs.append(spec)

    return specs


def _read_credential_types_folder(folder: Path) -> list[CredentialTypeSpec]:
    """Read `<folder>/credential_types/*.toml`. Each file's top level IS one
    credential type's fields (design §2) -- mirrors `_read_skills_folder`
    exactly: `[]` when the folder is absent, a `ManifestError` naming the
    offending file for anything malformed rather than a silent skip.
    """
    types_dir = folder / CREDENTIAL_TYPES_DIRNAME
    if not types_dir.is_dir():
        return []

    not_toml = sorted(
        p.name
        for p in types_dir.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.suffix != ".toml"
    )
    if not_toml:
        raise ManifestError(
            f"{types_dir}: unexpected file(s) {not_toml} -- every file in "
            "credential_types/ must be a <name>.toml whose top level is one "
            "credential type's fields (dotfiles like .DS_Store are silently ignored)"
        )

    specs: list[CredentialTypeSpec] = []
    for path in sorted(types_dir.glob("*.toml")):
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ManifestError(f"cannot read {path}: {exc}") from exc
        try:
            specs.append(CredentialTypeSpec.model_validate(raw))
        except ValidationError as exc:
            raise ManifestError(f"{path}: {exc}") from exc
    return specs


def _read_setup_folder(folder: Path) -> PluginSetupSpec | None:
    """Assemble `<folder>/setup/{fields,validation,oauth_provision,mcp}.toml`
    (design §3) into one `PluginSetupSpec`, exactly as if they had been one
    inline `[plugin.setup]` table. `None` when the folder is absent -- a
    plugin with no setup form behaves exactly as today.

    Only the four known filenames are read, so ANY other file in `setup/` is
    rejected rather than skipped: `oauth_provisioning.toml` (one typo'd letter)
    would otherwise discover perfectly valid with the entire OAuth block
    missing -- the same "silent skip" outcome the hard cutover exists to
    prevent, reached from the typo direction instead of the leftover
    direction."""
    setup_dir = folder / SETUP_DIRNAME
    if not setup_dir.is_dir():
        return None

    unexpected = sorted(
        p.name
        for p in setup_dir.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.name not in SETUP_FILENAMES
    )
    if unexpected:
        raise ManifestError(
            f"{setup_dir}: unexpected file(s) {unexpected} -- setup/ may contain only "
            f"{sorted(SETUP_FILENAMES)}; a misspelled name would be read by nobody and "
            "silently drop that part of the form (dotfiles like .DS_Store are silently ignored)"
        )

    def _load(name: str) -> dict[str, Any]:
        path = setup_dir / name
        if not path.is_file():
            return {}
        try:
            return tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ManifestError(f"cannot read {path}: {exc}") from exc

    if not (setup_dir / "fields.toml").is_file():
        raise ManifestError(
            f"{setup_dir}: setup/ exists but fields.toml is missing -- it carries "
            "the form's required `title`, so an assembled form without it would be "
            "silently untitled rather than an error"
        )

    fields_data = _load("fields.toml")
    unknown_keys = sorted(set(fields_data) - FIELDS_TOML_KEYS)
    if unknown_keys:
        raise ManifestError(
            f"{setup_dir / 'fields.toml'}: unknown key(s) {unknown_keys} -- fields.toml "
            f"carries only {sorted(FIELDS_TOML_KEYS)}; `validation`, `oauth_provision` "
            "and `mcp` each live in their own file in setup/, and a block written here "
            "instead would be silently discarded"
        )
    merged: dict[str, Any] = {
        "title": fields_data.get("title", ""),
        "description": fields_data.get("description", ""),
        "submit_label": fields_data.get("submit_label", "Save & test"),
        "fields": fields_data.get("fields", []),
        # NOT optional to carry through: `configure_plugin` calls this hook to
        # actually test a submitted credential. Two shipped plugins set it.
        "validate_entry_point": fields_data.get("validate_entry_point", ""),
    }
    validation_data = _load("validation.toml")
    if validation_data:
        merged["validation"] = validation_data
    oauth_provision_data = _load("oauth_provision.toml")
    if oauth_provision_data:
        merged["oauth_provision"] = oauth_provision_data
    mcp_data = _load("mcp.toml")
    if mcp_data:
        merged["mcp"] = mcp_data

    try:
        return PluginSetupSpec.model_validate(merged)
    except ValidationError as exc:
        raise ManifestError(f"{setup_dir}: {exc}") from exc


def _read_i18n_folder(folder: Path) -> dict[str, dict[str, str]]:
    """Load every `<folder>/i18n/<locale>.po` into `{locale: {msgid: msgstr}}`.
    `{}` when the folder is absent -- every existing plugin, forever, unless
    it adds one (design §4). Raises `ManifestError` on a malformed catalog so
    ONE bad plugin is invalidated, not the whole discovery scan."""
    import polib

    i18n_dir = folder / I18N_DIRNAME
    if not i18n_dir.is_dir():
        return {}
    out: dict[str, dict[str, str]] = {}
    for path in sorted(i18n_dir.glob("*.po")):
        locale = path.stem
        try:
            catalog = polib.pofile(str(path))
        except Exception as exc:  # polib raises bare IOError/ValueError variants
            raise ManifestError(f"invalid .po catalog {path}: {exc}") from exc
        out[locale] = {entry.msgid: entry.msgstr for entry in catalog if entry.msgstr}
    return out


def _has_inline_guardrail_presets(tool_pack_raw: dict[str, Any]) -> bool:
    """True when `tool_pack.toml` still carries `guardrail_presets` anywhere --
    at its top level or inside any connection."""
    if "guardrail_presets" in tool_pack_raw:
        return True
    connections = tool_pack_raw.get("connections")
    if isinstance(connections, list):
        return any(isinstance(c, dict) and "guardrail_presets" in c for c in connections)
    return False


def _attach_skills_to_table(table: dict[str, Any], skill_specs: list[SkillTemplateSpec]) -> None:
    if not skill_specs:
        return
    existing: list[dict[str, Any]] = []
    if table.get("skill_template"):
        existing.append(table["skill_template"])
    if table.get("skill_pack"):
        existing.extend(table["skill_pack"].get("skills") or [])
    seen = {s["name"] for s in existing}
    merged = list(existing)
    for spec in skill_specs:
        if spec.name in seen:
            raise ManifestError(f"duplicate skill name {spec.name!r}")
        seen.add(spec.name)
        merged.append(spec.model_dump(mode="json"))
    table.pop("skill_template", None)
    table.pop("skill_pack", None)
    table["skill_pack"] = {"skills": merged}


def _read_oc8_table(folder: Path, plugin_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse ``plugin.toml`` and oc8 sibling folders into a manifest table."""
    manifest_file = folder / MANIFEST_FILENAME
    if not manifest_file.is_file():
        return None, None
    try:
        raw = tomllib.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return None, f"cannot read {MANIFEST_FILENAME}: {exc}"

    table = raw.get(_ROOT_TABLE)
    if not isinstance(table, dict):
        return None, f"missing [{_ROOT_TABLE}] table"

    if "tool_pack" in table:
        return None, (
            "[plugin.tool_pack] has moved out of plugin.toml into a sibling "
            "tool_pack.toml (whose top level IS the tool_pack table)"
        )
    if "setup" in table:
        return None, (
            "[plugin.setup] has moved out of plugin.toml into setup/"
            "{fields,validation,oauth_provision,mcp}.toml"
        )
    if "skill_pack" in table or "skill_template" in table:
        return None, (
            "[plugin.skill_pack] / [plugin.skill_template] has moved out of "
            "plugin.toml into skills/<slug>.toml -- one file per skill, its "
            "top level IS the skill table"
        )
    if (folder / "guardrails.toml").is_file():
        return None, (
            "guardrails.toml has moved to guardrails/<key>.toml -- one file per "
            'entry, each carrying kind = "preset" or kind = "library"'
        )

    tool_pack_path = folder / TOOL_PACK_FILENAME
    if tool_pack_path.is_file():
        try:
            tool_pack_raw = tomllib.loads(tool_pack_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            return None, f"cannot read {TOOL_PACK_FILENAME}: {exc}"
        if _has_inline_guardrail_presets(tool_pack_raw):
            return None, (
                "guardrail_presets has moved out of tool_pack.toml into "
                'guardrails/<key>.toml -- one file per preset, each carrying kind = "preset" '
                "and the `connection` it binds to"
            )
        table = {**table, "tool_pack": tool_pack_raw}

    table = dict(table)
    table.setdefault("source_format", "oc8")
    return table, None


def _apply_guardrails_and_setup(
    folder: Path, plugin_id: str, table: dict[str, Any]
) -> tuple[dict[str, Any], GuardrailLibrary | None, str | None]:
    try:
        presets_by_connection, guardrail_library = _read_guardrails_folder(folder)
    except ManifestError as exc:
        return table, None, f"invalid guardrails/: {exc}"

    if presets_by_connection:
        tool_pack_table = table.get("tool_pack")
        if not isinstance(tool_pack_table, dict) or "connections" not in tool_pack_table:
            return (
                table,
                guardrail_library,
                "guardrails/ declares a preset for a connection, but this "
                "plugin has no [plugin.tool_pack] connections at all",
            )
        connections = tool_pack_table["connections"]
        if not isinstance(connections, list) or not all(isinstance(c, dict) for c in connections):
            return (
                table,
                guardrail_library,
                "tool_pack.toml's connections must be an array of tables -- write "
                f"[[connections]] (double brackets), not [connections]; got {connections!r}",
            )
        known_keys = [c.get("key", "default") for c in connections]
        unbound = presets_by_connection.pop(None, None)
        if unbound is not None:
            if len(known_keys) != 1:
                return (
                    table,
                    guardrail_library,
                    "guardrails/ has a preset with no `connection`, but this "
                    f"plugin declares {len(known_keys)} tool_pack connections "
                    f"({sorted(known_keys)}) -- name one explicitly",
                )
            presets_by_connection.setdefault(known_keys[0], []).extend(unbound)
        unknown = {k for k in presets_by_connection if k is not None and k not in known_keys}
        if unknown:
            return (
                table,
                guardrail_library,
                f"guardrails/ names unknown tool_pack connection(s): {sorted(unknown)}",
            )
        for conn in connections:
            key = conn.get("key", "default")
            if key in presets_by_connection:
                conn["guardrail_presets"] = [
                    p.model_dump(mode="json") for p in presets_by_connection[key]
                ]

    try:
        setup_spec = _read_setup_folder(folder)
    except ManifestError as exc:
        return table, guardrail_library, f"invalid setup/: {exc}"
    if setup_spec is not None:
        table = {**table, "setup": setup_spec.model_dump(mode="json")}

    return table, guardrail_library, None


def _read(folder: Path) -> DiscoveredPlugin:
    plugin_id = folder.name
    has_toml = (folder / MANIFEST_FILENAME).is_file()
    has_claude = is_claude_plugin(folder)

    if not has_toml and not has_claude:
        return _invalid(plugin_id, folder, "not a capa: no plugin.toml or Claude plugin layout")

    warnings: list[str] = []
    guardrail_library: GuardrailLibrary | None = None

    try:
        if has_claude and has_toml:
            claude_table, claude_warnings = adapt_claude_plugin(folder, plugin_id=plugin_id)
            warnings.extend(claude_warnings)
            oc8_table, oc8_err = _read_oc8_table(folder, plugin_id)
            if oc8_err:
                return _invalid(plugin_id, folder, oc8_err)
            assert oc8_table is not None
            table = merge_claude_and_oc8(claude_table, oc8_table)
        elif has_claude:
            table, claude_warnings = adapt_claude_plugin(folder, plugin_id=plugin_id)
            warnings.extend(claude_warnings)
        else:
            oc8_table, oc8_err = _read_oc8_table(folder, plugin_id)
            if oc8_err:
                return _invalid(plugin_id, folder, oc8_err)
            assert oc8_table is not None
            table = oc8_table
    except ManifestError as exc:
        return _invalid(plugin_id, folder, str(exc))

    table, guardrail_library, apply_err = _apply_guardrails_and_setup(folder, plugin_id, table)
    if apply_err:
        return _invalid(plugin_id, folder, apply_err)

    try:
        skill_specs = _read_skills_folder(folder)
    except ManifestError as exc:
        return _invalid(plugin_id, folder, f"invalid skills/: {exc}")
    try:
        _attach_skills_to_table(table, skill_specs)
    except ManifestError as exc:
        return _invalid(plugin_id, folder, str(exc))

    try:
        credential_type_specs = _read_credential_types_folder(folder)
    except ManifestError as exc:
        return _invalid(plugin_id, folder, f"invalid credential_types/: {exc}")
    if credential_type_specs:
        table = {
            **table,
            "credential_types": [s.model_dump(mode="json") for s in credential_type_specs],
        }

    try:
        manifest = parse_manifest(table)
    except ManifestError as exc:
        return _invalid(plugin_id, folder, f"invalid manifest: {exc}")

    if manifest.name != plugin_id:
        return _invalid(
            plugin_id,
            folder,
            f"folder name {plugin_id!r} does not match manifest name {manifest.name!r}",
        )

    try:
        i18n = _read_i18n_folder(folder)
    except ManifestError as exc:
        return _invalid(plugin_id, folder, f"invalid i18n/: {exc}")

    return DiscoveredPlugin(
        plugin_id=plugin_id,
        path=str(folder),
        manifest=manifest.model_dump(mode="json"),
        name=manifest.name,
        version=manifest.version,
        type=manifest.type,
        trust=manifest.trust,
        summary=manifest.summary,
        valid=True,
        error=None,
        guardrail_library=guardrail_library,
        i18n=i18n,
        warnings=warnings,
    )


#: `discover_plugins` rescans every root and re-parses every manifest on each
#: call, and it is called once per plugin for things like icon lookups
#: (`GET /capas/{id}/icon`) -- a Capas list page with N rows fires N of these
#: near-simultaneously, each redoing the SAME full scan the previous one just
#: did. Plugin folders only change via deployment (a container rebuild or a
#: volume mount), never through anything the running app does at runtime
#: (`install_from_disk` only registers a DB row for a plugin already on disk,
#: it does not write files) -- so a short TTL cache collapses a same-page
#: request storm into one real scan while keeping any staleness window (an
#: operator dropping a new plugin folder in) too small to matter in practice.
_DISCOVERY_CACHE_TTL_SECONDS = 5.0
_discovery_cache: dict[tuple[str, ...], tuple[float, list[DiscoveredPlugin]]] = {}


def _iter_capa_dirs(base: Path) -> list[Path]:
    """Every directory under ``base`` that is a capa root.

    Native oc8 capas carry ``plugin.toml``. Claude Agent SDK plugins may ship
    without it (``.claude-plugin/plugin.json``, ``skills/*/SKILL.md``, …).
    Nested catalog layout (``capas/agents/<dept>/<id>/``) is supported.
    """
    found: list[Path] = []

    def is_capa_root(dir_path: Path) -> bool:
        if (dir_path / MANIFEST_FILENAME).is_file():
            return True
        return is_claude_plugin(dir_path)

    def walk(dir_path: Path) -> None:
        try:
            children = sorted(p for p in dir_path.iterdir() if p.is_dir())
        except OSError:
            return
        for child in children:
            if is_capa_root(child):
                found.append(child)
                continue
            walk(child)

    walk(base)
    return found


def discover_plugins(paths: Sequence[str] | None = None) -> list[DiscoveredPlugin]:
    """Scan the configured roots for plugin folders.

    A missing root is not an error (an operator may configure a path that isn't
    mounted yet). When the same plugin id appears in several roots the FIRST
    root wins and the later one is shadowed, mirroring an addons-path search
    order. Results are sorted by plugin id.

    Cached for `_DISCOVERY_CACHE_TTL_SECONDS` per resolved root set -- see the
    cache's own comment above for why that's safe.
    """
    roots = list(paths) if paths is not None else get_settings().capas_path_list
    cache_key = tuple(roots)
    cached = _discovery_cache.get(cache_key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _DISCOVERY_CACHE_TTL_SECONDS:
        return cached[1]

    out: dict[str, DiscoveredPlugin] = {}
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        for folder in _iter_capa_dirs(base):
            if folder.name in out:
                logger.info("plugin %s in %s shadowed by an earlier root", folder.name, root)
                continue
            out[folder.name] = _read(folder)

    for plugin_id, cycle_path in _find_plugin_depends_cycles(out).items():
        out[plugin_id] = _invalid(
            plugin_id,
            Path(out[plugin_id].path),
            f"plugin_depends cycle: {' -> '.join(cycle_path)}",
        )

    result = [out[key] for key in sorted(out)]
    _discovery_cache[cache_key] = (now, result)
    return result


def invalidate_discovery_cache() -> None:
    """Clear the TTL cache immediately. Not on any production call path today
    (nothing at runtime writes new plugin folders -- see the cache's own
    comment); exists so a test that writes to the SAME root across multiple
    `discover_plugins()`/`find_plugin()` calls within one test function can
    force a fresh scan instead of waiting out the TTL."""
    _discovery_cache.clear()


def _find_plugin_depends_cycles(
    plugins: dict[str, DiscoveredPlugin],
) -> dict[str, list[str]]:
    """Map every plugin id that participates in at least one `plugin_depends`
    cycle to THE ACTUAL PATH of the cycle it was found in, using a plain
    3-colour DFS. Only edges to plugins that were themselves discovered are
    followed -- a dependency on a plugin absent from disk is not a
    cycle-detection concern (it's an install-time error, Step 4).

    Per-plugin paths, not one merged set: joining every plugin in ANY cycle
    into one string and stamping it on all of them turns two disjoint
    2-cycles into a bogus 4-plugin "cycle" reported four times, which sends
    an author looking at plugins that have nothing to do with their mistake.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = dict.fromkeys(plugins, WHITE)
    in_cycle: dict[str, list[str]] = {}

    def edges(plugin_id: str) -> list[str]:
        discovered = plugins[plugin_id]
        if not discovered.valid or discovered.manifest is None:
            return []
        deps = discovered.manifest.get("plugin_depends") or []
        return [d for d in deps if d in plugins]

    def visit(plugin_id: str, stack: list[str]) -> None:
        color[plugin_id] = GRAY
        stack.append(plugin_id)
        for dep in edges(plugin_id):
            if color[dep] == GRAY:
                cycle_start = stack.index(dep)
                path = [*stack[cycle_start:], dep]
                for member in path[:-1]:
                    in_cycle.setdefault(member, path)
            elif color[dep] == WHITE:
                visit(dep, stack)
        stack.pop()
        color[plugin_id] = BLACK

    for plugin_id in plugins:
        if color[plugin_id] == WHITE:
            visit(plugin_id, [])
    return in_cycle


def find_plugin(plugin_id: str, paths: Sequence[str] | None = None) -> DiscoveredPlugin | None:
    for plugin in discover_plugins(paths):
        if plugin.plugin_id == plugin_id:
            return plugin
    return None


def on_disk_deferred_permissions(name: str) -> set[str]:
    """What the manifest on disk defers for capa ``name`` (allow-listed).

    A capa's code is loaded from disk, while its installed version may be an
    older one that predates the deferral. The code that would run declares
    what it needs consent for, so its deferral counts until the tenant updates.
    """
    discovered = find_plugin(name)
    if discovered is None or discovered.manifest is None:
        return set()
    declared = set(discovered.manifest.get("deferred_permissions") or [])
    return declared & DEFERRABLE_PERMISSIONS


def resolve_tool_pack_connection(
    plugin_name: str, connection_key: str
) -> ToolPackConnection | None:
    """The manifest `ToolPackConnection` a materialised `McpConnection` row was
    stamped from -- `materialise.py` writes `_plugin_name`/`_connection_key`
    onto every row it creates, and this is the read side of that pair.

    Exists because plugin-authored data (the `scopes` read/write/send
    classification in particular -- see `oc8.authz.pdp.required_right`) lives
    only on the manifest's `ToolPackConnection`, never copied onto the row:
    `McpConnection.scopes` is an unrelated column (a flat list, "standardised"
    per its own docstring, predating this classification) that happens to
    share the name. A caller that reads `McpConnection.scopes` expecting the
    read/write/send dict gets `None` back from a plain list every time --
    always fail-closed to `write` in `required_right`, invisibly so as long as
    a connection's frame grants `write`, and outright broken the moment it
    does not (which is every real guardrail preset this design ships, since
    none of them grants `write`).

    None for a connection whose plugin was removed from disk, ships no tool
    pack, or no longer declares this connection key -- callers must tolerate
    that rather than crash a run over a missing plugin folder.
    """
    if not plugin_name:
        return None
    discovered = find_plugin(plugin_name)
    if discovered is None or not discovered.valid or discovered.manifest is None:
        return None
    try:
        manifest = parse_manifest(discovered.manifest)
    except ManifestError:
        return None
    if manifest.tool_pack is None:
        return None
    return next(
        (conn for conn in manifest.tool_pack.connections if conn.key == connection_key),
        None,
    )
    return None


def connection_supports_value_spec(conn: ToolPackConnection | None) -> bool:
    """Whether a manifest connection declares a value_spec -- the gate behind
    `approval_eur` on a submitted tool policy (a monetary threshold needs to
    know where a record's amount lives). NOT a gate for `only`: that's a
    plain tool-name allowlist, meaningful for any connection regardless of
    value_spec -- github_mcp/jira_mcp/microsoft365/google_workspace all ship
    real `only`-based guardrail presets with no value_spec at all. None (no
    manifest, plugin removed from disk, or the connection key no longer
    exists) fails closed to False -- `approval_eur` is not supported without
    a resolvable manifest to prove otherwise.
    """
    return conn is not None and "value_spec" in conn.config
