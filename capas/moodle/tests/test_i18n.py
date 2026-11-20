from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import polib

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _toml(relative: str) -> dict[str, Any]:
    return tomllib.loads((PLUGIN_ROOT / relative).read_text())


def _visible_strings() -> set[str]:
    out: set[str] = set()
    plugin = _toml("plugin.toml")["plugin"]
    out |= {plugin["label"], plugin["summary"]}
    fields = _toml("setup/fields.toml")
    out |= {fields["title"], fields["description"], fields["submit_label"]}
    for field in fields["fields"]:
        out |= {field["label"], field["help"]}
    cred = _toml("credential_types/moodle_token.toml")
    out.add(cred["display_name"])
    for field in cred["fields"]:
        out |= {field["label"], field["help"]}
    for path in (PLUGIN_ROOT / "guardrails").glob("*.toml"):
        entry = tomllib.loads(path.read_text())
        out |= {entry["label"], entry["summary"]}
    conn = _toml("tool_pack.toml")["connections"][0]
    for label in conn["tool_labels"].values():
        out |= {v for k, v in label.items() if k in ("verb", "object", "running") and v}
    focus = conn["config"]["focus_spec"]
    out |= set(focus["labels"].values()) | set(focus["verbs"].values())
    out |= {focus["default_verb"], focus["search_label"]}
    return out


def test_every_visible_string_has_a_german_translation() -> None:
    catalog = polib.pofile(str(PLUGIN_ROOT / "i18n" / "de.po"))
    translated = {e.msgid for e in catalog if e.msgstr and "fuzzy" not in e.flags}
    missing = sorted(_visible_strings() - translated)
    assert not missing, f"untranslated: {missing}"


def test_placeholders_survive_translation() -> None:
    catalog = polib.pofile(str(PLUGIN_ROOT / "i18n" / "de.po"))
    for entry in catalog:
        for token in ("{course_id}", "{user_id}"):
            assert (token in entry.msgid) == (token in entry.msgstr), entry.msgid
