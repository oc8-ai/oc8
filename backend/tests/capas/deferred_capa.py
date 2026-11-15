"""A fixture capa written into a tmp folder: one trivial connector, the
permissions a test asks for, optionally some of them deferred.

Shared by the deferred-permission tests in ``tests/capas`` and
``tests/knowledge``; rewriting the manifest is how a test ships an update.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oc8 import config
from oc8.capas import contributions, loader
from oc8.capas.discovery import invalidate_discovery_cache

NAME = "deferred_demo"
TYPE_ID = "deferred_demo"

_CONNECTOR = """
from __future__ import annotations

from typing import Any

from oc8.knowledge.connectors.base import ValidationResult


class DeferredDemoConnector:
    type_id = "deferred_demo"
    label = "Deferred demo"
    description = "A connector behind a deferred permission"
    config_schema: dict[str, Any] = {"type": "object", "properties": {}}
    requires_oauth: str | None = None

    async def validate(self, config, auth=None):
        return ValidationResult(ok=True)

    async def discover(self, config, auth=None):
        return []

    async def fetch(self, config, cursor, auth=None):
        if False:
            yield None


def register(contrib: Any) -> None:
    contrib.add_connector(DeferredDemoConnector())
"""


def write_capa(
    root: Path,
    *,
    version: str = "1.0.0",
    permissions: list[str],
    deferred: list[str] | None = None,
) -> None:
    folder = root / NAME
    (folder / "connector").mkdir(parents=True, exist_ok=True)
    (folder / "connector" / "__init__.py").write_text("")
    (folder / "connector" / "connector.py").write_text(_CONNECTOR)
    perms = ", ".join(f'"{p}"' for p in permissions)
    lines = [
        "[plugin]",
        f'name = "{NAME}"',
        f'version = "{version}"',
        'type = "connector"',
        'trust = "first_party"',
        'summary = "Test fixture: a connector behind a deferred permission"',
        f"permissions = [{perms}]",
    ]
    if deferred is not None:
        lines.append("deferred_permissions = [" + ", ".join(f'"{p}"' for p in deferred) + "]")
    lines += ["", "[plugin.entry_points]", 'connectors = "connector.connector:register"', ""]
    (folder / "plugin.toml").write_text("\n".join(lines))
    invalidate_discovery_cache()


def use_capas_path(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OC8_CAPAS_PATH", str(root))
    config.get_settings.cache_clear()
    invalidate_discovery_cache()
    loader.reset_for_tests()
    contributions.reset_for_tests()


def reset() -> None:
    config.get_settings.cache_clear()
    invalidate_discovery_cache()
    loader.reset_for_tests()
    contributions.reset_for_tests()
