"""Proves the retrofitted `provider/` package is importable at its new path and
still registers -- through the REAL loader, so the `plugin.toml` entry-point
string is proved too.

This plugin had no test of any kind before the restructure (verified: `find
backend/tests -iname "*opaas*"` was empty), which meant the one thing this
retrofit changes -- `providers = "provider.provider:register"` -- had nothing
watching it. One test, going through `find_plugin`/`load_plugin` rather than
importing `provider.provider` directly, is what makes a typo in that string a
red test instead of a silent no-op at runtime (Task 11's review, Important #3).
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from oc8.capas import contributions, loader
from oc8.capas.discovery import find_plugin
from oc8.config import get_settings

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = PLUGIN_ROOT.parent


def _evict() -> None:
    """Drop every cached `provider`/`provider.*` module from sys.modules.

    `provider` is the folder convention for a `model_adapter` plugin (design
    §2), so it is a name any future model adapter also ships, and sys.modules
    is keyed by NAME, not by path. Called SYMMETRICALLY on both sides of the
    fixture's yield: before, so a sibling's cached copy cannot answer the
    loader's import; after, so nothing generic is left cached --
    `loader.import_entry_point` (Task 6's collision fix) only evicts modules IT
    ITSELF introduced, so a `provider` left cached here would make the next
    model adapter's own load silently hand back THIS plugin's `register`.
    """
    for _stale in [n for n in sys.modules if n == "provider" or n.startswith("provider.")]:
        del sys.modules[_stale]


@pytest.fixture(autouse=True)
def _plugin_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Task 7's `_plugin_path` convention plus the loader/settings reset this
    test needs, since it drives real discovery rather than a bare import."""
    monkeypatch.setenv("OC8_CAPAS_PATH", str(PLUGINS_ROOT))
    get_settings.cache_clear()
    loader.reset_for_tests()
    contributions.reset_for_tests()
    _evict()
    sys.path.insert(0, str(PLUGIN_ROOT))
    yield
    sys.path.remove(str(PLUGIN_ROOT))
    _evict()
    loader.reset_for_tests()
    contributions.reset_for_tests()
    get_settings.cache_clear()


def test_the_manifest_entry_point_really_loads_and_registers_the_provider() -> None:
    found = find_plugin("opaas_ai_provider")
    assert found is not None, "opaas_ai_provider was not discovered at all"
    assert found.valid, found.error
    assert found.manifest is not None
    assert found.manifest["entry_points"]["providers"] == "provider.provider:register"

    assert loader.load_plugin(found) is True
    assert "opaas_ai" in contributions.providers_for("opaas_ai_provider")


def test_the_credential_type_is_discovered() -> None:
    """The BYOK key/base_url a tenant enters for this provider goes through
    the unified credentials framework (a `Credential` of THIS plugin's own
    `opaas_ai_api_key` type, credential_types/opaas_ai_api_key.toml) --
    `resolve_model_key`/`resolve_model_base_url` (oc8.modelrouter.keys)
    already resolve any `{canonical}_api_key` type generically by name, so
    the one thing this plugin still has to supply for that resolution to
    ever find a row is the type declaration itself."""
    found = find_plugin("opaas_ai_provider")
    assert found is not None
    assert found.valid, found.error
    assert found.manifest is not None
    types = found.manifest["credential_types"]
    assert [t["name"] for t in types] == ["opaas_ai_api_key"]
    fields = {f["key"]: f for f in types[0]["fields"]}
    assert fields["api_key"]["kind"] == "password"
    assert fields["base_url"]["kind"] == "url"
    assert fields["base_url"]["default"] == "https://ai.opaas.online/v1"


def test_display_names_say_oc8_ai_powered_by_opaas() -> None:
    import pathlib
    import tomllib

    root = pathlib.Path(__file__).resolve().parents[1]
    plugin = tomllib.loads((root / "plugin.toml").read_text())
    credential = tomllib.loads((root / "credential_types" / "opaas_ai_api_key.toml").read_text())
    assert plugin["plugin"]["summary"].startswith("oc8 AI – powered by opaas")
    assert credential["display_name"] == "oc8 AI – powered by opaas"
