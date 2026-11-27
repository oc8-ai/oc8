"""The run reach's forcing function: what an agent did, read by a person.

Same sweep as `tests/agents/test_reads_go_through_the_scoped_repository.py`
(its `_loads` walker, given this sweep's own guarded sets), for spec A4: the
runs, run files, memory and activity of an agent in a personal department are
readable only by its owner. `agents.repo.run_reach` carries that rule
(`RunReach.private`, `departments.repo.is_private_to`) and its readers
(`restrict_to_run_reach`, `run_reachable_agent`, `chat.reach.
reaches_agent_runs`) apply it; this sweep makes reading one of these rows
anywhere else a decision somebody writes a sentence for.

Only the trees a person's request reaches are swept -- `api/`, `chat/`,
`realtime/` and `copilot/`. The runtime reading its OWN run, memory and files
(`agent/`, `runtime/`, `memory/`, `metering/`) has no human reader in the
path and is out of scope here, as it is in the plan's ruling for this task.

`KNOWN_UNFIXED` holds readers that are NOT yet private-safe and that a later
task of the PR 3 plan owns: each entry names the task. It shrinks as those
tasks land; `test_known_unfixed_gaps_are_still_named_here` keeps it honest.
"""

from __future__ import annotations

import pathlib

from tests.agents.test_reads_go_through_the_scoped_repository import _loads

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "oc8"

#: The trees a human request reaches. A guarded read outside them is the
#: runtime's own, which this sweep does not judge.
SWEPT = ("api", "chat", "realtime", "copilot")

#: What a personal agent produced (spec A4): its runs, its activity, its
#: memory, its files.
GUARDED = frozenset({"AgentRun", "ActivityEvent", "MemoryRecord", "FileAttachment"})
GUARDED_TABLES = frozenset({"agent_run", "activity_event", "memory_record", "file_attachment"})

#: Path (relative to `src/oc8`) -> why it may load one.
ALLOWED: dict[str, str] = {
    # --- Through the run reach (`RunReach.private` applies).
    "api/v1/run.py": (
        "every read goes through restrict_to_run_reach or run_reachable_agent "
        "(GET /runs, /runs/{id}, /agents/{id}/runs, /agents/{id}/workspace/files); "
        "the cancel's reach keeps private runs (U1) but answers them with "
        "_stopped_dto, ids and state only"
    ),
    "api/v1/feed.py": (
        "GET /activity narrows through restrict_to_run_reach, in the statement "
        "before the limit, so a private department's activity never reaches a page"
    ),
    "api/v1/reports.py": (
        "GET /reports narrows the rendered-output runs through restrict_to_run_reach "
        "on top of visible_agents; a personal agent's reports are its owner's alone"
    ),
    "api/v1/files.py": (
        "GET /files/{id} loads the row by id, then member_may_open_attachment decides: "
        "a run's file goes through run_reach + run_reachable_agent (private applied)"
    ),
    "chat/context/sources.py": (
        "FileResolver.load fetches by id and authorize() is member_may_open_attachment, "
        "the files route's own gate; search lists the viewer's own chat uploads only"
    ),
    "chat/context/objects.py": (
        "RunResolver.load fetches by id and authorize() admits the viewer's own run or "
        "chat, else asks reaches_agent_runs, which applies RunReach.private"
    ),
    "chat/live.py": (
        "timeline_runs: the caller's own runs, plus the autonomous ones only when "
        "reaches_agent_runs (private applied) said the caller reaches the agent's runs"
    ),
    "chat/watch.py": (
        "may_watch_run: the member's own run or chat, or an autonomous run only while "
        "member_reaches_agent_runs (private applied) holds, and always agent reach"
    ),
    "api/v1/run_inputs.py": (
        "_steerable loads the run by id, then may_steer_run decides: the caller's own "
        "run, or an autonomous one only with run:control reach (private applied)"
    ),
    "api/v1/agents_write.py": (
        "DELETE /agents/{id}/memory/{record_id} loads the record only after "
        "authorize_agent_write, which refuses a personal agent to all but its owner "
        "(is_private_to); answers 204, no content"
    ),
    "api/v1/_serializers.py": (
        "resolve_approval_names decorates cards visible_approvals/load_for_actor already "
        "admitted (a personal agent's card to its owner alone); the reply_to excerpt and "
        "run ids are read only for runs of those cards' own agents"
    ),
    # --- Metadata only (ruling R2): ids and state, never content.
    "api/v1/agents.py": (
        "the agent detail's current_run is a run id only (R2 metadata); the "
        "instruction files are read after _refuse_personal_content refused a "
        "non-owner, and are agent_instructions, not run output"
    ),
    "copilot/followups.py": (
        "_session_busy asks whether a run of the member's own session is open: one "
        "id, never content, for that member's own follow-up scheduling"
    ),
    "copilot/profile.py": (
        "pause and status read the ids of runs answering the member's OWN Copilot "
        "sessions, to stop or count them; never another person's, never content"
    ),
    # --- The caller's own conversation or the agent token's own run.
    "api/llm_gateway.py": "agent-token-authenticated path, scoped to the token's own run",
    "api/mcp_gateway.py": "agent-token-authenticated path, scoped to the token's own run",
    "api/v1/internal_agent.py": (
        "_run_for_token: an agent token reads only the run its run:<id> scope names"
    ),
    "api/v1/chat.py": (
        "every read is of the caller's own (or readably shared) chat session, after "
        "_owned_session/_readable_session: the session's uploads, a clarification's "
        "run of that session, and a run whose context names that session"
    ),
    "api/v1/copilot_member.py": (
        "the runs delegated from the member's OWN Assistant sessions (chat_session_id "
        "in their sessions), each started through the start reach that never admits "
        "somebody else's personal agent"
    ),
    "chat/service.py": (
        "reads attachments of the session or message being sent or copied and the "
        "open runs of one session, for the session's own owner; no cross-person read"
    ),
    "chat/export.py": (
        "attachment_names lists the file names of the chat's own uploads (chat_message, "
        "never a run's files or their text) for GET /chat/sessions/{id}/export, after "
        "_readable_session; a reader of a shared chat gets them redacted"
    ),
    "chat/replay.py": (
        "the chat replay a run of that chat is built from (agent/preamble.py, chat/"
        "checkpoint.py): the names of the chat's own uploads and the state of the runs "
        "its answers name; no human request reaches it"
    ),
    "chat/checkpoint.py": (
        "the background checkpoint job loads the one run whose reply it summarises, "
        "scheduled by chat/service.py for that run's own chat; no human request reaches it"
    ),
    "chat/files_kb.py": (
        "the chat's own uploads: chat_files_on_path for the chat run's own tool "
        "(search_chat_files), finish_chat_file for the ingestion worker, forget_chat_file "
        "for DELETE /files/{id} after member_may_open_attachment admitted the chat's owner"
    ),
    "chat/carry_over.py": (
        "run_still_open reads one run's state under its row lock for the finished "
        "run's own follow-up turn; state only, after the owner's reach was re-checked"
    ),
    "copilot/builder_tools.py": (
        "the Copilot's builder tool reads its OWN current run's context (door_of) to "
        "refuse a non-web door; the run id comes from the executing run itself"
    ),
    "copilot/direct_access.py": (
        "the Copilot's own run (offer) and the parent run a person's own direct-access "
        "card names (decision), checked against the card's task; no other person's run"
    ),
    "copilot/notes.py": (
        "the member's own Copilot notes (record_metadata member_id == the member) and "
        "the Copilot's own run, to refuse an operator writing a colleague's notes"
    ),
    "copilot/tools.py": (
        "the Copilot's own executing run's context (door_of), for its own tool call; "
        "no human reads another person's run through it"
    ),
}

#: Path -> the later PR 3 task that makes the read private-safe. Not ALLOWED:
#: these reads are NOT fine yet; they are named so the sweep stays green while
#: the finding stays visible.
KNOWN_UNFIXED: dict[str, str] = {}


def _swept_modules() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for tree in SWEPT:
        out.extend(p for p in (SRC / tree).rglob("*.py") if "__pycache__" not in p.parts)
    return sorted(out)


def _guarded_loads(source: str, filename: str = "<test>") -> list[tuple[int, str]]:
    return _loads(source, filename=filename, guarded=GUARDED, guarded_tables=GUARDED_TABLES)


def test_the_sweep_actually_sees_what_it_is_looking_for() -> None:
    assert _guarded_loads("rows = await db.execute(select(m.AgentRun))") == [(1, "AgentRun")]
    assert _guarded_loads("r = await db.get(m.AgentRun, run_id)") == [(1, "AgentRun")]
    assert _guarded_loads("q = select(m.ActivityEvent.id)") == [(1, "ActivityEvent")]
    assert _guarded_loads("q = select(aliased(MemoryRecord))") == [
        (1, "MemoryRecord"),
        (1, "MemoryRecord"),
    ]
    assert _guarded_loads("f = await s.get(FileAttachment, x)") == [(1, "FileAttachment")]
    assert _guarded_loads('await db.execute(text("SELECT * FROM agent_run"))') == [(1, "agent_run")]
    # And the negatives.
    assert _guarded_loads("q = select(m.Agent).where(m.AgentRun.id == x)") == []
    assert _guarded_loads("t = payload.get('agent_run')") == []


def test_the_sweep_is_reading_the_real_tree() -> None:
    modules = _swept_modules()
    assert len(modules) > 50, f"only found {len(modules)} modules under {SWEPT}"
    # A live positive control: GET /runs really does select AgentRun.
    loaded = {model for _line, model in _guarded_loads((SRC / "api/v1/run.py").read_text())}
    assert "AgentRun" in loaded and "FileAttachment" in loaded


def test_known_unfixed_gaps_are_still_named_here() -> None:
    """A gap that gets fixed must leave this set, or it hides the next one."""
    for rel, reason in KNOWN_UNFIXED.items():
        assert "Task " in reason, f"{rel}: name the PR 3 task that fixes it"
        known = SRC / rel
        assert known.is_file(), f"KNOWN_UNFIXED names {rel}, which no longer exists"
        loaded = {model for _line, model in _guarded_loads(known.read_text())}
        assert loaded & GUARDED, (
            f"{rel} no longer loads a guarded model raw -- its KNOWN_UNFIXED entry is "
            "stale and should be removed"
        )
    assert not set(KNOWN_UNFIXED) & set(ALLOWED), "a path is either fine or a known gap"


def test_no_person_reads_what_an_agent_did_outside_the_run_reach() -> None:
    offenders: list[str] = []
    for path in _swept_modules():
        rel = path.relative_to(SRC).as_posix()
        if rel in ALLOWED or rel in KNOWN_UNFIXED:
            continue
        for line, model in _guarded_loads(path.read_text(), filename=str(path)):
            offenders.append(f"src/oc8/{rel}:{line} loads {model}")

    assert not offenders, (
        "these read a run, its activity, memory or files without going through the "
        "run reach, so an admin could read a personal agent's content:\n  "
        + "\n  ".join(offenders)
        + "\n\nNarrow through `agents.repo.restrict_to_run_reach` / "
        "`run_reachable_agent` (or `departments.repo.is_private_to`), or add the "
        "module to ALLOWED with the sentence that justifies it."
    )


def test_the_allowlist_names_swept_files_that_exist() -> None:
    missing = [rel for rel in ALLOWED if not (SRC / rel).is_file()]
    assert not missing, f"allowlisted modules that no longer exist: {missing}"
    outside = [rel for rel in ALLOWED if rel.split("/", 1)[0] not in SWEPT]
    assert not outside, f"allowlisted modules outside the swept trees: {outside}"


def test_every_allowlisted_module_says_why() -> None:
    reasons = [*ALLOWED.values(), *KNOWN_UNFIXED.values()]
    assert all(len(reason.strip()) >= 20 for reason in reasons), (
        "an allowlist entry is only worth having if it carries a reason a "
        "reviewer can disagree with"
    )
