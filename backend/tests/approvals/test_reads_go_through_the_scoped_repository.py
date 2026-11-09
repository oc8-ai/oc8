"""The read term's forcing function.

Modelled on `tests/api/test_every_route_is_governed.py`, and for the same reason
that file exists: the permission layer was not missing because anyone decided
those routes should be open, it was missing because nothing ever forced a new
route to say. Here the thing nothing forces is narrower and worse -- a new
screen, a new export, a new digest e-mail writes `select(m.ApprovalRequest)`,
gets every department in the tenant, and looks exactly like the code beside it.

Both source designs left this to convention. Convention is what
`api/v1/feed.py:66` is: a list filtered on status alone that has handed back the
whole company's approvals since the day it was written.

So a human-facing load of an `ApprovalRequest` or a `Clarification` is legal in
one of a few named modules and nowhere else. The allowlist is short on purpose --
each entry is a claim that the module either resolves a department itself or has
no human in it at all, and adding one is that claim being made out loud.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "oc8"

#: The two rows a person may not read across departments. `AgentRun`, `Task` and
#: the transcript are slice 2 (§10 item 1) -- they are reachable only through
#: `run:view` / `department:view`, which no seat carries, so a seat-holder 403s
#: at all of them today.
GUARDED = frozenset({"ApprovalRequest", "Clarification"})

#: Path (relative to `src/oc8`) -> why it may load one.
ALLOWED: dict[str, str] = {
    "approvals/repo.py": "the scoped repository itself -- this is the door",
    "approvals/service.py": "the decide/raise funnel, which takes the actor and checks it",
    "workspace/queue.py": "the clarification half of the same door",
    "runtime/approval_resume.py": "resumes a run from an ALREADY-decided approval; no human here",
    "runtime/clarification.py": "resolves the run's own open question; no human here",
    # `_push_payload`'s DB fallback reads the row to build a push notification's
    # title/body -- not to show it to any one actor. The Web Push feature's own
    # Global Constraint is that delivery is tenant-wide, matching the existing
    # WebSocket broadcast's own scope: EXPLICITLY no per-department filtering of
    # who receives it. Routing this through the actor-scoped repository would be
    # wrong here, not just extra ceremony -- there is no actor to scope it to.
    "realtime/bus.py": "builds a tenant-wide push payload; no human viewer to scope it to",
    # Not in §8's list, and admitted deliberately: `executor.py` re-reads the
    # tool_send approval for the run it is executing, to see whether the call it
    # is about to retry was already answered. There is no person in that path and
    # the agent runtime must stay byte-identical in this slice -- binding a human
    # scope anywhere near it is what breaks the 3000-EUR gate the product is
    # demonstrated on.
    "runtime/executor.py": "the agent runtime reading its own held call; no human here",
    "metering/budget.py": "raises the tenant-scope incident and checks it is not already open",
    # The chat answer doors read a run's OWN open question, reached through a
    # run or session the caller already owns (`_owned_session`, the run's tenant
    # check, or the channel's own sessions) -- they lock the row to serialise two
    # tabs answering at once, which the scoped listing cannot do.
    "api/v1/chat.py": "locks the open question of the caller's own chat session to answer it",
    "api/v1/run.py": "locks the open question of a tenant-checked run to answer it",
    "chat/service.py": "finds the open question of the runs of a session the caller owns",
    "channels/dispatch.py": "finds the open question of a run in the channel member's own sessions",
}


#: The tables behind `GUARDED`. Raw SQL does not mention the mapped class at all,
#: so the AST walk below cannot see it -- and `text("SELECT … FROM
#: approval_request")` reads every department exactly as `select(ApprovalRequest)`
#: does. Named here so the two spellings are guarded by one list.
GUARDED_TABLES = frozenset({"approval_request", "clarification"})

#: Calls whose first argument names a guarded model and which therefore load one.
#: `aliased` is in the list because `select(aliased(ApprovalRequest))` is the
#: obvious way to write a self-join and was invisible to the first version of this
#: sweep, which matched `select(...)` and `.get(...)` alone.
_LOADERS = frozenset({"select", "aliased"})


def _loads(source: str, *, filename: str = "<test>") -> list[tuple[int, str]]:
    """Every load of a guarded model, however it is spelled.

    An AST walk rather than a regex, because the shapes that matter are exactly
    the ones a regex misses: `select(\\n    m.ApprovalRequest,\\n)` across three
    lines, `session.get(ApprovalRequest, id)` under a different name for `db`,
    and `select(ApprovalRequest.id)` which enumerates the same rows one column at
    a time.

    Only the arguments to the call itself are inspected, never the chained
    `.where(...)`, so `select(m.Agent).where(m.ApprovalRequest.agent_id == ...)`
    is not a load and is not flagged.

    Raw SQL is caught separately, by the table name inside any string literal
    handed to `text(...)`. Crude on purpose: this is a forcing function, and a
    false positive costs one allowlist line with a sentence on it, while a false
    negative costs a screen that shows the whole company's approvals.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        args: list[ast.expr]
        if isinstance(func, ast.Name) and func.id in _LOADERS:
            args = list(node.args)
        elif isinstance(func, ast.Attribute) and func.attr in _LOADERS:
            args = list(node.args)
        elif isinstance(func, ast.Attribute) and func.attr == "get":
            args = list(node.args[:1])
        elif (isinstance(func, ast.Name) and func.id == "text") or (
            isinstance(func, ast.Attribute) and func.attr == "text"
        ):
            for table in _guarded_tables_in(node.args):
                found.append((node.lineno, table))
            continue
        else:
            continue
        for arg in args:
            model = _guarded_model(arg)
            if model is not None:
                found.append((node.lineno, model))
    return found


def _guarded_tables_in(args: list[ast.expr]) -> list[str]:
    """Guarded table names appearing in any string literal argument."""
    out: list[str] = []
    for arg in args:
        for sub in ast.walk(arg):
            if not isinstance(sub, ast.Constant) or not isinstance(sub.value, str):
                continue
            lowered = sub.value.lower()
            out.extend(
                table for table in sorted(GUARDED_TABLES) if table in lowered and table not in out
            )
    return out


def _guarded_model(expr: ast.expr) -> str | None:
    for sub in ast.walk(expr):
        if isinstance(sub, ast.Name) and sub.id in GUARDED:
            return sub.id
        if isinstance(sub, ast.Attribute) and sub.attr in GUARDED:
            return sub.attr
    return None


def test_the_sweep_actually_sees_what_it_is_looking_for() -> None:
    """The first version of the route sweep this is modelled on returned zero
    routes and would therefore have passed every assertion under it."""
    assert _loads("rows = await db.execute(select(m.ApprovalRequest))") == [(1, "ApprovalRequest")]
    assert _loads("ar = await db.get(m.ApprovalRequest, approval_id)") == [(1, "ApprovalRequest")]
    assert _loads("ar = await session.get(ApprovalRequest, x)") == [(1, "ApprovalRequest")]
    assert _loads("rows = select(\n    Clarification,\n)") == [(1, "Clarification")]
    assert _loads("q = select(m.ApprovalRequest.id).where(m.ApprovalRequest.status == 'x')") == [
        (1, "ApprovalRequest")
    ]
    # And the negatives, so the sweep is not simply "anything mentioning it".
    assert _loads("q = select(m.Agent).where(m.ApprovalRequest.agent_id == a.id)") == []
    assert _loads("t = payload.get('clarification')") == []
    assert _loads("d = run.context.get('clarifications', [])") == []


def test_the_sweep_sees_the_three_spellings_that_used_to_slip_past_it() -> None:
    """Every one of these reads every department in the tenant, and every one of
    them returned `[]` from the first version of this sweep -- whose own docstring
    claimed to catch "the shapes a regex misses".

    They are not hypothetical spellings: `aliased` is how a self-join is written
    in this repository, and `text(...)` is how `audit/chain.py` and
    `metering/budget.py` already talk to the database.
    """
    assert _loads("q = select(aliased(ApprovalRequest))") == [
        (1, "ApprovalRequest"),
        (1, "ApprovalRequest"),
    ]
    assert _loads('rows = await db.execute(text("SELECT * FROM approval_request"))') == [
        (1, "approval_request")
    ]
    assert _loads('rows = await db.execute(sa.text("select id from clarification"))') == [
        (1, "clarification")
    ]
    # Still not "anything mentioning the word".
    assert _loads('await db.execute(text("SELECT 1"))') == []
    assert _loads('log.info("clarification answered")') == []


def test_the_sweep_is_reading_the_real_tree() -> None:
    modules = [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]
    assert len(modules) > 100, f"only found {len(modules)} modules under {SRC}; the walk is broken"

    # A live positive control that survives the implementation: this module
    # really does load a Clarification, and it is on the allowlist. If the walker
    # ever stops seeing it, every assertion below becomes vacuous.
    known = SRC / "runtime" / "clarification.py"
    assert any(model == "Clarification" for _line, model in _loads(known.read_text())), (
        "runtime/clarification.py loads a Clarification and the sweep no longer sees it"
    )
    assert "runtime/clarification.py" in ALLOWED


def test_no_module_loads_an_approval_or_a_clarification_outside_the_repo() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(SRC).as_posix()
        if rel in ALLOWED:
            continue
        for line, model in _loads(path.read_text(), filename=str(path)):
            offenders.append(f"src/oc8/{rel}:{line} loads {model}")

    assert not offenders, (
        "these load a human-visible row without going through the scoped "
        "repository, so they see every department in the tenant:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse `approvals.repo.load_for_actor` / `visible_approvals` or "
        "`workspace.queue.open_clarifications`, or add the module to ALLOWED "
        "with the sentence that justifies it."
    )


def test_the_allowlist_names_files_that_exist() -> None:
    """A stale entry is a hole that reads as documentation. `workspace/queue.py`
    is created by this slice; everything else is here already."""
    missing = [rel for rel in ALLOWED if not (SRC / rel).is_file()]
    assert not missing, f"allowlisted modules that no longer exist: {missing}"


def test_every_allowlisted_module_says_why() -> None:
    assert all(len(reason.strip()) >= 20 for reason in ALLOWED.values()), (
        "an allowlist entry is only worth having if it carries a reason a "
        "reviewer can disagree with"
    )
