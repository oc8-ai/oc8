"""A migration never imports the application's models: a revision is
history, and a later rename or change of a model must not change what an
already-applied revision does when it runs on another database."""

from __future__ import annotations

import ast
from pathlib import Path

_VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"


def _imports_models(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("oc8.models"):
            return True
        if isinstance(node, ast.Import) and any(
            alias.name.startswith("oc8.models") for alias in node.names
        ):
            return True
    return False


def test_no_migration_imports_the_models() -> None:
    offenders = sorted(p.name for p in _VERSIONS.glob("*.py") if _imports_models(p))
    assert not offenders, f"migrations importing oc8.models: {offenders}"
