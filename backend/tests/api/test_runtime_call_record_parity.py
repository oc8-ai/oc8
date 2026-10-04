"""Both runtimes write the same per-step record (run step timeline, Task 5).

This is the test the harness lessons say will otherwise break. The preamble,
the control-tool dispatcher and the tool-call timing have each been added to
the in-process engine and silently missed in the container path; the
`state`/`step`/`connection` trio is the fourth such addition, and this asserts
the two paths against ONE expectation rather than each against its own.

It reads both source files rather than running two agents: the runtime that
is missing a key is missing it at its append SITE, and that is a property of
the code, cheap and deterministic to assert. The behavioural halves live in
tests/agents/test_engine_call_record_fields.py and
tests/api/test_internal_agent_call_record_fields.py.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from oc8.runtime.step_record import REQUIRED_CALL_KEYS

_SRC = Path(__file__).resolve().parents[2] / "src" / "oc8"
_ENGINE = _SRC / "agent" / "engine.py"
_ISOLATED = _SRC / "api" / "v1" / "internal_agent.py"


def _call_record_literals(path: Path) -> list[dict[str, object]]:
    """Every dict literal in `path` that looks like a tool-call record, i.e.
    has "tool", "arguments" AND "step" keys. Returns {key: None} maps -- only
    the key sets matter here.

    "tool" + "arguments" alone is not selective enough: both files also build
    unrelated dict literals carrying exactly those two keys (or those two plus
    a couple more), e.g. `append_event`'s `resource={"tool": ..., "arguments":
    ...}`, `raise_approval`'s payload (adds "justification"/"preview"), and
    `pending_elicitation` (adds "connection"/"question") -- none of these are
    `toolCalls` entries. "step" is the one key every genuine entry carries
    (it is the whole reason this module exists) and none of the false
    positives do, so requiring it as well isolates the real record sites.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[dict[str, object]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {
            k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        if {"tool", "arguments", "step"} <= keys:
            found.append(dict.fromkeys(keys))
    return found


@pytest.mark.parametrize("path", [_ENGINE, _ISOLATED], ids=["in_process", "isolated"])
def test_every_call_record_site_writes_every_required_key(path: Path) -> None:
    records = _call_record_literals(path)
    assert records, f"no tool-call record literal found in {path} -- did the shape move?"
    for keys in records:
        missing = REQUIRED_CALL_KEYS - set(keys)
        assert not missing, f"{path.name}: a call record is missing {sorted(missing)}"


def test_both_runtimes_have_the_same_number_of_record_sites() -> None:
    """Not a blind equality: the in-process engine has one more record site
    than the isolated runtime, and that asymmetry is real and justified, not
    drift. The engine has a plugin-hook (PreToolUse) blocking path with its
    own dedicated append -- blocked-by-hook, parked-for-approval, and the
    shared append, three sites. `internal_agent.py` has no PreToolUse/plugin
    hook dispatch at all (`grep -n PreToolUse src/oc8/api/v1/internal_agent.py`
    finds nothing), so it only ever has parked-for-approval and the shared
    append, two sites. If a THIRD site ever appears in the isolated runtime,
    or the engine's count changes, this should be re-examined rather than
    the numbers bumped blindly."""
    assert len(_call_record_literals(_ENGINE)) == 3
    assert len(_call_record_literals(_ISOLATED)) == 2
