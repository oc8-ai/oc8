"""The read term's forcing function, for `Agent` and `Department`.

Modelled on `tests/approvals/test_reads_go_through_the_scoped_repository.py`,
for the identical reason that file exists: nothing forces a new screen, a new
export, or a new digest e-mail that writes `select(m.Agent)` to stop and ask
whether the caller may see every department in the tenant. `agents/repo.py` and
`departments/repo.py` are the door this design adds; this sweep is what makes
walking around it a decision somebody has to write a sentence for, not a typo
nobody notices.

Two things differ from the approvals sweep, and both are decisions made out
loud rather than gaps:

* `metering/budget.py` is allowlisted here too, for the SAME reason the
  approvals sweep allowlists it: it raises and reads its own tenant-scope
  incident and has no human caller.
* `agent/preamble.py` is named and DELIBERATELY NOT allowlisted, even though it
  loads its own agent's department roster (`roster_block`) with no human in the
  path -- the design's own test-plan entry calls this out by name
  ("explicitly excluded"). Leaving it off is a live claim that the scoped
  repository, or a narrower loader, is where that read belongs once the
  migration lands; pinned by `test_preamble_is_deliberately_not_allowlisted` so
  a future edit that quietly adds it back has to say why, the same way adding
  any other entry does.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "oc8"

#: The two rows a person may see only in the departments a live seat (or an
#: unrestricted grant) actually reaches.
GUARDED = frozenset({"Agent", "Department"})

#: Path (relative to `src/oc8`) -> why it may load one.
ALLOWED: dict[str, str] = {
    "agents/repo.py": "the scoped repository itself -- this is the door",
    "departments/repo.py": "the scoped repository itself -- this is the door",
    "api/v1/agents.py": "the read routes this design wires to the funnel",
    "api/v1/departments.py": "the read routes this design wires to the funnel",
    "api/v1/agents_write.py": (
        "_load_agent, and create_agent's own department-existence check -- both "
        "authorized by authorize_agent_write before any frame-derived computation"
    ),
    "api/v1/catalog.py": "a different permission (catalog:view), id-only reads",
    "api/v1/skills_write.py": "loads the agent it is about to assign a skill to",
    "metering/budget.py": "raises and reads its own tenant-scope incident; no human here",
    "api/v1/contracts.py": "department-name resolution for the contract screen",
    "api/v1/handoffs.py": "department-name resolution for both sides of a handoff",
    "api/v1/members.py": "checks a department exists before a seat is written into it",
    "api/v1/triggers.py": "loads the agent a schedule or cron is being attached to",
    "triggers/service.py": (
        "create_trigger's manual-only-subscription check re-reads the same agent "
        "the route already loaded, only to read model_config_id for a safety "
        "decision -- not a departmental browse; the Copilot's own call into this "
        "function is already gated the same way copilot/capabilities.py is above"
    ),
    "api/v1/run.py": "loads the agent that owns the run being started or controlled",
    "api/v1/knowledge.py": "resolves the grantee (agent or department) of a KB share",
    "api/v1/components.py": "resolves the grantee (agent or department) of a component grant",
    "agent/control_tools.py": "the agent runtime reading its own department; no human here",
    "kpis/aggregate.py": (
        "id-only reads (Agent.id/Agent.department_id, never content): the KPI endpoints "
        "prove visibility with require_departmental + visible_agent/visible_department "
        "BEFORE calling compute_kpis, so the funnel has already run by the time this does"
    ),
    "api/v1/kpis.py": (
        "id-only enumeration of the tenant's own agents/departments for groupBy, behind "
        "statistics:view (a tenant-wide permission by design); the agent- and department-"
        "scoped routes in the same file go through visible_agent/visible_department. "
        "Registered even though an earlier revision of this file passed the sweep by "
        "accident -- it bound the column to a local (`id_col = m.Agent.id`) before calling "
        "select(id_col), and the walk below only inspects a call's literal arguments, not "
        "what a name was assigned from; the exception is meant to be a decision, not a "
        "side effect of how the query happened to be spelled"
    ),
    "api/v1/feed.py": (
        "GET /usage/export outer-joins Agent/Department only to NAME the agent and "
        "department on each token-usage row -- behind budget:view via require_permission, "
        "which admits a tenant-wide hold only (no seat reaches it), and over exactly the "
        "rows GET /usage in the same file already aggregates tenant-wide for that "
        "permission; not a departmental browse"
    ),
    # --- Everything below is the disclosed remainder of the sweep's first
    # real run against Agent/Department: agent-runtime-internal reads, system
    # jobs, and two sweep false-positives on a substring match. None of these
    # are a human departmental browse -- each is checked individually below.
    "agent/assistant.py": (
        "get_or_create_assistant looks up the tenant's single is_tenant_assistant-"
        "flagged agent by that flag, provisioning it if missing -- a system "
        "provisioning helper with no human caller in the path, not a departmental browse"
    ),
    "agent/claims.py": "the runtime resolving a record-claim holder's display name",
    "agent/engine.py": "the agent runtime loading its OWN department's tool frame",
    "agents/hire.py": "hire_agent effect resolution acts on the approval's own bound agent",
    "api/llm_gateway.py": "agent-token-authenticated path, scoped to the token's own run",
    "api/mcp_gateway.py": "agent-token-authenticated path, scoped to the token's own run",
    "api/v1/capas.py": "a different permission (plugin install), department-existence check only",
    "api/v1/internal_agent.py": "agent-token-authenticated path, scoped to the token's own run",
    "capas/export.py": (
        "gated by plugin:manage -- the SAME tenant-wide permission api/v1/capas.py above "
        "already uses for capa install/enable, not a narrower departmental seat. An "
        "operator packaging a Department/Agent/Skill they already administer into a "
        "portable capa is the same 'admin already sees every department' shape as "
        "copilot/capabilities.py below, just reading full content instead of an id"
    ),
    "approvals/service.py": "approval-effect resolution acting on the approval's own bound agent",
    "copilot/capabilities.py": (
        "gated by copilot:manage (NEVER_DELEGATABLE, org_admin only); an admin "
        "already sees every department, so this is not a departmental browse"
    ),
    "chat/service.py": (
        "send_message loads only the ChatSession's own session.agent_id, to check "
        "is_tenant_assistant for the Assistant-only secret gate -- not a departmental "
        "browse; that agent's visibility is already established by the caller "
        "(api/v1/chat.py's ownership/_assistant_visible check) before send_message runs"
    ),
    "collab/emit.py": "handoff department-name resolution, same rationale as api/v1/handoffs.py",
    "collab/intake.py": "handoff department-name and team-lead resolution, no human in this path",
    "evidence/sweep.py": "background retention sweep on its own tenant scope; no human here",
    "models/ops.py": (
        "sweep false-positive: 'department_id' is a column name inside index DDL text, "
        "not a read of the Department table"
    ),
    "runtime/executor.py": "the agent runtime loading its OWN agent row to run or reap it",
    "runtime/reconcile.py": "background reconciliation loading its own stale agent rows",
    "runtime/repository.py": (
        "RunRepository.create reads only the target agent's current_version_id to pin "
        "the run it creates; every caller has already authorized starting that agent"
    ),
    "runtime/run_context.py": (
        "sweep false-positive: raw SQL against the agent_run table, whose name merely "
        "contains 'agent' as a substring"
    ),
    "tenants/cli.py": "operator CLI tooling, not an HTTP route a seated caller can reach",
    "workspace/members.py": (
        "resolves the department NAME for seats the member already holds, for their own "
        "workspace page -- not a departmental browse"
    ),
    "workspace/queue.py": (
        "the clarification queue applies scope.viewable itself (see its own department_id."
        "in_(scope.viewable) filter) instead of going through the funnel"
    ),
}

#: Path -> why it is a PINNED, DISCLOSED gap rather than an ALLOWED exception.
#: Deliberately its own set, not folded into ALLOWED: ALLOWED means "this read
#: is fine, permanently, and the sentence says why"; this means "this read is
#: NOT fine, is not fixed by this design, and a future slice owns fixing it."
#: `test_preamble_is_deliberately_not_allowlisted` pins that `agent/preamble.py`
#: stays OUT of `ALLOWED` specifically so a future editor who reaches for the
#: easy fix (add it to ALLOWED) has to read that test's docstring first. This
#: set is what keeps the sweep itself green in the meantime without silently
#: dropping the finding -- `test_known_unfixed_gaps_are_still_named_here`
#: below is the forcing function that keeps THIS set honest.
KNOWN_UNFIXED: dict[str, str] = {
    "agent/preamble.py": (
        "roster_block's colleague query -- the design's own test-plan entry names "
        "this EXPLICITLY EXCLUDED, not migrated, in this slice; see "
        "test_preamble_is_deliberately_not_allowlisted"
    ),
}

#: The tables behind `GUARDED`. Raw SQL does not mention the mapped class at
#: all, so the AST walk below cannot see it -- named here so both spellings are
#: guarded by one list, same as the approvals sweep.
GUARDED_TABLES = frozenset({"agent", "department"})

#: Calls whose first argument names a guarded model and which therefore load one.
_LOADERS = frozenset({"select", "aliased"})


def _loads(source: str, *, filename: str = "<test>") -> list[tuple[int, str]]:
    """Every load of a guarded model, however it is spelled. See the approvals
    sweep's docstring for the shapes this exists to catch; unchanged here."""
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
    """The approvals sweep's first version returned zero routes and would
    therefore have passed every assertion under it -- this file's own positive
    control against the same failure."""
    assert _loads("rows = await db.execute(select(m.Agent))") == [(1, "Agent")]
    assert _loads("a = await db.get(m.Agent, agent_id)") == [(1, "Agent")]
    assert _loads("d = await session.get(Department, x)") == [(1, "Department")]
    assert _loads("rows = select(\n    Department,\n)") == [(1, "Department")]
    assert _loads("q = select(m.Agent.id).where(m.Agent.status == 'x')") == [(1, "Agent")]
    # And the negatives.
    assert _loads("q = select(m.Task).where(m.Agent.id == a.assigned_agent_id)") == []
    assert _loads("t = payload.get('agent')") == []
    assert _loads("d = run.context.get('department', {})") == []


def test_the_sweep_sees_the_three_spellings_that_used_to_slip_past_it() -> None:
    assert _loads("q = select(aliased(Agent))") == [(1, "Agent"), (1, "Agent")]
    assert _loads('rows = await db.execute(text("SELECT * FROM agent"))') == [(1, "agent")]
    assert _loads('rows = await db.execute(sa.text("select id from department"))') == [
        (1, "department")
    ]
    # Still not "anything mentioning the word".
    assert _loads('await db.execute(text("SELECT 1"))') == []
    assert _loads('log.info("department renamed")') == []


def test_the_sweep_is_reading_the_real_tree() -> None:
    modules = [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]
    assert len(modules) > 100, f"only found {len(modules)} modules under {SRC}; the walk is broken"

    # A live positive control that survives the implementation: this module
    # really does load an Agent AND a Department outside the repo, and it is on
    # the allowlist. If the walker ever stops seeing it, the assertions below
    # are vacuous.
    known = SRC / "agent" / "control_tools.py"
    loaded = {model for _line, model in _loads(known.read_text())}
    assert "Agent" in loaded and "Department" in loaded, (
        "agent/control_tools.py loads both and the sweep no longer sees them"
    )
    assert "agent/control_tools.py" in ALLOWED


def test_preamble_is_deliberately_not_allowlisted() -> None:
    """`agent/preamble.py` loads an `Agent` (`roster_block`'s colleague query)
    and the design's own test-plan entry names it as EXPLICITLY EXCLUDED from
    this list -- not overlooked. Pinned so a future edit that quietly adds it
    back has to make that decision out loud, the same as any other entry."""
    assert "agent/preamble.py" not in ALLOWED
    known = SRC / "agent" / "preamble.py"
    assert known.is_file(), "the file this exclusion is about no longer exists"
    loaded = {model for _line, model in _loads(known.read_text())}
    assert "Agent" in loaded, (
        "agent/preamble.py no longer loads an Agent raw -- the exclusion this "
        "test pins may now be moot and worth revisiting, not silently dropping"
    )


def test_known_unfixed_gaps_are_still_named_here() -> None:
    """The forcing function for `KNOWN_UNFIXED` itself: a gap that gets fixed
    (the file stops loading `Agent`/`Department` raw) must have its entry
    removed here, or this set silently starts hiding a NEW, unrelated gap
    reusing the same path -- exactly the failure mode `ALLOWED`'s own
    `test_the_allowlist_names_files_that_exist` guards against."""
    for rel in KNOWN_UNFIXED:
        known = SRC / rel
        assert known.is_file(), f"KNOWN_UNFIXED names {rel}, which no longer exists"
        loaded = {model for _line, model in _loads(known.read_text())}
        assert loaded & GUARDED, (
            f"{rel} no longer loads a guarded model raw -- KNOWN_UNFIXED's entry for "
            "it is stale and should be removed, not left to hide the next regression"
        )


def test_no_module_loads_an_agent_or_department_outside_the_scoped_repository() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(SRC).as_posix()
        if rel in ALLOWED or rel in KNOWN_UNFIXED:
            continue
        for line, model in _loads(path.read_text(), filename=str(path)):
            offenders.append(f"src/oc8/{rel}:{line} loads {model}")

    assert not offenders, (
        "these load a human-visible Agent or Department without going through "
        "the scoped repository, so they see every department in the tenant:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse `agents.repo.visible_agent(s)` / `departments.repo.visible_"
        "department(s)`, or add the module to ALLOWED with the sentence that "
        "justifies it."
    )


def test_the_allowlist_names_files_that_exist() -> None:
    """`agents/repo.py` and `departments/repo.py` are created by this design;
    everything else is here already. A stale entry is a hole that reads as
    documentation."""
    missing = [rel for rel in ALLOWED if not (SRC / rel).is_file()]
    assert not missing, f"allowlisted modules that no longer exist: {missing}"


def test_every_allowlisted_module_says_why() -> None:
    assert all(len(reason.strip()) >= 20 for reason in ALLOWED.values()), (
        "an allowlist entry is only worth having if it carries a reason a "
        "reviewer can disagree with"
    )
