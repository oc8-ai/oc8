"""The read term's forcing function for data boards.

Same sweep as `tests/agents/test_reads_go_through_the_scoped_repository.py`
(its `_loads` walker, given this sweep's own guarded sets): nothing outside
`boards/repo.py` may load a board, its records, batches or spec versions without
a sentence saying why it may see every department's.
"""

from __future__ import annotations

import ast
import pathlib

from tests.agents.test_reads_go_through_the_scoped_repository import _loads

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "oc8"

GUARDED = frozenset({"DataBoard", "DataBoardRecord", "DataBoardBatch", "DataBoardSpecVersion"})
GUARDED_TABLES = frozenset(
    {"data_board", "data_board_record", "data_board_batch", "data_board_spec_version"}
)

#: Path (relative to `src/oc8`) -> why it may load one. `boards/lifecycle.py`
#: is listed below.
ALLOWED: dict[str, str] = {
    "boards/repo.py": "the scoped repository itself -- this is the door",
    "boards/records.py": (
        "the write path (board_write): reads its own agent's board under a row lock, "
        "and the effective-batch rules; no human caller"
    ),
    "boards/query.py": (
        "reads the records and batches of a board the caller was already admitted to "
        "through boards.repo; the board row is passed in"
    ),
    "boards/mission.py": (
        "syncs the mission section from the producing agent's own live boards; "
        "no human caller, no departmental browse"
    ),
    "copilot/capabilities.py": (
        "board.create/board.update: the board's revision and its kept spec versions, "
        "applied only through builder proposals (generic proposals refuse them), whose "
        "department rule decided reach"
    ),
    "copilot/builder.py": (
        "applies a builder proposal's board after may_view/may_create_agent or the "
        "approval funnel decided reach; at propose time reads a named board only to "
        "refuse one outside the member's may_view departments as board_unknown, and "
        "reads its department and title for the card, the approval and the notice"
    ),
    "copilot/builder_tools.py": (
        "builder tools read the board a proposal names, behind the proposal's owner "
        "and department gates"
    ),
    "api/v1/copilot_builder.py": (
        "the builder card reads the board a proposal created, only after the owner or "
        "submitted+may_view gate passed, tenant-checked"
    ),
    "boards/lifecycle.py": (
        "archives and restores a department's boards and deletes a board's history; "
        "called by routes that already authorized the department or the board"
    ),
    "api/v1/boards.py": (
        "the read routes: they load records and batches only for boards boards.repo "
        "already admitted the caller to (aggregate counts and the query engine)"
    ),
}


def _aliases(source: str, *, filename: str = "<test>") -> list[tuple[int, str]]:
    """`b = m.DataBoardBatch`: binding a guarded model to a name, which `_loads` cannot
    follow into a later `select(b)`."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not isinstance(node, ast.Assign | ast.AnnAssign) or node.value is None:
            continue
        value = node.value
        if isinstance(value, ast.Name) and value.id in GUARDED:
            found.append((node.lineno, value.id))
        elif isinstance(value, ast.Attribute) and value.attr in GUARDED:
            found.append((node.lineno, value.attr))
    return found


def _boards_loads(source: str, *, filename: str = "<test>") -> list[tuple[int, str]]:
    return sorted(
        _loads(source, filename=filename, guarded=GUARDED, guarded_tables=GUARDED_TABLES)
        + _aliases(source, filename=filename)
    )


def test_the_sweep_sees_board_loads() -> None:
    assert _boards_loads("select(m.DataBoard)") == [(1, "DataBoard")]
    assert _boards_loads("db.get(m.DataBoardRecord, x)") == [(1, "DataBoardRecord")]
    assert _boards_loads('db.execute(text("select 1 from data_board_batch"))') == [
        (1, "data_board"),
        (1, "data_board_batch"),
    ]
    assert _boards_loads("b = m.DataBoardBatch") == [(1, "DataBoardBatch")]
    assert _boards_loads("b: type = DataBoard") == [(1, "DataBoard")]
    assert _boards_loads("select(m.Agent)") == []
    assert _loads("select(m.DataBoard)") == []  # the agents sets do not guard boards


def test_the_sweep_is_reading_the_real_tree() -> None:
    known = SRC / "boards" / "mission.py"
    loaded = {model for _line, model in _boards_loads(known.read_text())}
    assert loaded & GUARDED, "boards/mission.py loads board rows and the sweep no longer sees it"


def test_no_module_loads_a_board_outside_the_scoped_repository() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(SRC).as_posix()
        if rel in ALLOWED:
            continue
        for line, model in _boards_loads(path.read_text(), filename=str(path)):
            offenders.append(f"src/oc8/{rel}:{line} loads {model}")
    assert not offenders, (
        "these load a board without going through boards.repo, so they see every "
        "department's:\n  " + "\n  ".join(offenders)
    )


def test_the_allowlist_names_files_that_exist() -> None:
    missing = [rel for rel in ALLOWED if not (SRC / rel).is_file()]
    assert not missing, f"allowlisted modules that do not exist: {missing}"


def test_every_allowlisted_module_says_why() -> None:
    assert all(len(reason.strip()) >= 20 for reason in ALLOWED.values())
