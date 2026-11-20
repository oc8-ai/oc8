"""The migration chain is one straight line.

Two branches that each add "the next" migration land on the same revision id;
alembic then keeps only one of the files in its revision map and reports two
heads, and `alembic upgrade head` -- the compose `migrate` service and this
suite's own session fixture -- refuses to run at all. This has happened twice
(0093, 0098); these checks read the files, so they name the collision instead
of failing every test in the suite at setup.
"""

import ast
from collections import defaultdict
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

_BACKEND = Path(__file__).resolve().parents[2]
_VERSIONS = _BACKEND / "migrations" / "versions"


def _revision_of(path: Path) -> str | None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign | ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "revision"
                for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
            )
            and isinstance(node.value, ast.Constant)
        ):
            return str(node.value.value)
    return None


def test_every_revision_id_is_used_by_exactly_one_file() -> None:
    files_by_revision: dict[str, list[str]] = defaultdict(list)
    for path in sorted(_VERSIONS.glob("*.py")):
        revision = _revision_of(path)
        if revision is not None:
            files_by_revision[revision].append(path.name)

    duplicates = {rev: names for rev, names in files_by_revision.items() if len(names) > 1}
    assert not duplicates, f"revision ids used by more than one migration: {duplicates}"


def test_the_chain_has_a_single_head() -> None:
    script = ScriptDirectory.from_config(Config(str(_BACKEND / "alembic.ini")))

    assert len(script.get_heads()) == 1, f"migration chain has several heads: {script.get_heads()}"
