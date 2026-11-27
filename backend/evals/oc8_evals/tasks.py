"""Task declarations (spec §9.2, TOML instead of YAML -- no new dependency).

A task is static data plus the name of a scenario module that owns the four
lifecycle hooks (setup / expect / forbid / teardown). Everything the agent
sees is `task_text`; everything the scorer sees comes from the hooks reading
the target system -- never the transcript.

`kind = "chat"` makes the attempt a chat turn instead of a fixture run: the
harness opens a member's chat session first (`stack.create_chat_fixture`),
`setup` seeds it, and `task_text` is sent as the member's next turn -- unless
the scenario defines the optional `start` hook, which starts the turn itself
(an edit, a regenerate, a turn that is decided mid-way) and returns its run
id. For a chat the "target system" is the chat's own state: stored replies,
checkpoints, approvals, usage rows. `[params]` is free-form data the scenario
reads from `ctx.task.params`, so one scenario module serves a group of tasks.
"""

from __future__ import annotations

import importlib
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    # `scoring` shipped in Task 2, so this import now resolves and needs no
    # ignore; `stack` ships in Task 7, so it also resolves now.
    from oc8_evals.scoring import Check
    from oc8_evals.stack import ScenarioContext


@dataclass(frozen=True)
class Task:
    id: str
    systems: tuple[str, ...]
    task_text: str
    origin: str
    scenario: str
    autonomy: str
    frame_tools: dict[str, dict[str, bool | int]]
    expects_question: bool
    expects_approval: bool
    max_steps: int
    max_tokens: int
    code_mode: bool
    context_window_tokens: int | None
    skill_definition: dict[str, Any] | None = None
    #: "run" (a fixture agent's run, the default) or "chat" (a member's chat turn).
    kind: str = "run"
    #: Groups the report's rows (e.g. the chat_harness suite's six groups).
    group: str = ""
    params: dict[str, Any] = field(default_factory=dict)


class Scenario(Protocol):
    setup: Callable[[ScenarioContext], Awaitable[dict[str, Any]]]
    expect: Callable[[ScenarioContext, dict[str, Any]], Awaitable[list[Check]]]
    forbid: Callable[[ScenarioContext, dict[str, Any]], Awaitable[list[Check]]]
    teardown: Callable[[ScenarioContext, dict[str, Any]], Awaitable[None]]


def load_task(path: Path) -> Task:
    raw = tomllib.loads(path.read_text())
    if raw["id"] != path.stem:
        raise ValueError(f"file name {path.name!r} must match id {raw['id']!r}")
    budget = raw.get("budget", {})
    # Preserve TOML's native types -- do NOT coerce every value to bool.
    # approval_eur (odoo's monetary threshold, e.g. task 8's
    # sales_quotation_over_threshold) is an int; bool(3000) would silently
    # become True and the eval would never park for approval.
    frame_tools = {str(k): dict(v.items()) for k, v in raw.get("frame_tools", {}).items()}
    window = raw.get("context_window_tokens")
    context_window_tokens = (
        int(window)
        if isinstance(window, int) and not isinstance(window, bool) and window > 0
        else None
    )
    skill_raw = raw.get("skill")
    skill_definition = dict(skill_raw) if isinstance(skill_raw, dict) else None
    kind = str(raw.get("kind", "run"))
    if kind not in ("run", "chat"):
        raise ValueError(f"{path.name}: kind must be run|chat, not {kind!r}")
    params_raw = raw.get("params")
    return Task(
        id=str(raw["id"]),
        systems=tuple(str(s) for s in raw["systems"]),
        task_text=str(raw["task_text"]).strip(),
        origin=str(raw.get("origin", "manual")),
        scenario=str(raw["scenario"]),
        autonomy=str(raw.get("autonomy", "autonomous")),
        frame_tools=frame_tools,
        expects_question=bool(raw.get("expects_question", False)),
        expects_approval=bool(raw.get("expects_approval", False)),
        max_steps=int(budget.get("max_steps", 40)),
        max_tokens=int(budget.get("max_tokens", 300_000)),
        code_mode=bool(raw.get("code_mode", False)),
        context_window_tokens=context_window_tokens,
        skill_definition=skill_definition,
        kind=kind,
        group=str(raw.get("group", "")),
        params=dict(params_raw) if isinstance(params_raw, dict) else {},
    )


def load_suite(root: Path) -> list[Task]:
    return sorted((load_task(p) for p in root.glob("*.toml")), key=lambda t: t.id)


def starter(
    scenario: Scenario,
) -> Callable[[ScenarioContext, dict[str, Any]], Awaitable[Any]] | None:
    """A chat scenario's optional `start` hook, or None to send `task_text`."""
    hook = getattr(scenario, "start", None)
    return hook if callable(hook) else None


def resolve_scenario(name: str) -> Scenario:
    module = importlib.import_module(f"oc8_evals.scenarios.{name}")
    return cast(Scenario, module)
