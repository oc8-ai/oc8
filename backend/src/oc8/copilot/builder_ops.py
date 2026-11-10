"""Forward references inside one builder proposal (Copilot Agent Builder §3.2).

A builder proposal creates exactly one agent and configures it in the same
step. The first operation is `agent.create` with `ref: "$new"`; every later
operation names that agent with `agentRef: "$new"` instead of an `agentId`,
because the id does not exist until apply. This module is pure: it validates
the shape, gives the storage form and resolves a reference at apply time. The
closed operation schemas stay `capabilities.py`'s -- every step is parsed by
`parse_operations` with a placeholder id, so a builder step can carry nothing
an ordinary operation could not.

A builder proposal may only ever touch the agent it creates: a raw `agentId`
is refused on every step, at validation and again at resolve time, and a ref
resolves only to an `agent.create` earlier in the same proposal.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from oc8.copilot.capabilities import (
    InvalidOperation,
    Operation,
    operation_data,
    parse_operations,
)

#: The operations that may follow `agent.create` and point at it.
REF_TARGETS: Final = frozenset(
    {
        "agent.mission.set",
        "agent.model.switch",
        "agent.skill.assign",
        "trigger.create",
        "agent.guardrail.set",
        "agent.narrowing.set",
    }
)
#: What an `agentRef` step is validated with before the agent exists.
PLACEHOLDER_AGENT_ID: Final = uuid.UUID(int=0)
#: One agent with its settings; anything longer is not a builder proposal.
MAX_BUILDER_STEPS: Final = 24
_CREATE: Final = "agent.create"
_REF = re.compile(r"^\$[a-z][a-z0-9_]{0,31}$")


class BuilderInvalid(ValueError):
    """A builder proposal refused with a value-free code and, when one step is
    to blame, its index. Never carries submitted text."""

    def __init__(self, code: str, step: int | None = None) -> None:
        super().__init__(f"agent builder proposal refused: {code}")
        self.code = code
        self.step = step


@dataclass(frozen=True)
class BuilderStep:
    operation: Operation
    #: Set on the `agent.create` step: the name later steps use.
    ref: str | None = None
    #: Set on every later step: the `ref` it points at.
    agent_ref: str | None = None


def _is_ref(value: object) -> bool:
    return isinstance(value, str) and _REF.match(value) is not None


def parse_builder_operations(raw: object) -> list[BuilderStep]:
    """Validate a builder proposal. Rules: exactly one `agent.create`, first,
    carrying a `ref`; every later step is a `REF_TARGETS` type pointing at that
    ref with `agentRef` and no `agentId`; each step passes its closed schema."""
    if not isinstance(raw, list) or not raw or len(raw) > MAX_BUILDER_STEPS:
        raise BuilderInvalid("invalid")
    if not all(isinstance(item, dict) for item in raw):
        raise BuilderInvalid("invalid")
    if not any(item.get("type") == _CREATE for item in raw):
        raise BuilderInvalid("missing_create")
    declared_anywhere = {item.get("ref") for item in raw if _is_ref(item.get("ref"))}
    defined: set[str] = set()
    steps: list[BuilderStep] = []
    for index, item in enumerate(raw):
        data = dict(item)
        ref = data.pop("ref", None)
        agent_ref = data.pop("agentRef", None)
        kind = data.get("type")
        if kind == _CREATE:
            if ref is None:
                raise BuilderInvalid("missing_ref", index)
            if not _is_ref(ref) or agent_ref is not None:
                raise BuilderInvalid("invalid", index)
            if defined:
                raise BuilderInvalid("two_refs", index)
            defined.add(ref)
        else:
            if ref is not None:
                raise BuilderInvalid("invalid", index)
            if kind not in REF_TARGETS:
                raise BuilderInvalid("type_not_allowed", index)
            if "agentId" in data:
                raise BuilderInvalid("agent_id_not_allowed", index)
            if not _is_ref(agent_ref):
                raise BuilderInvalid("unknown_ref", index)
            if agent_ref not in defined:
                code = "ref_before_create" if agent_ref in declared_anywhere else "unknown_ref"
                raise BuilderInvalid(code, index)
            data["agentId"] = str(PLACEHOLDER_AGENT_ID)
        try:
            operation = parse_operations([data])[0]
        except InvalidOperation as exc:
            raise BuilderInvalid("invalid", index) from exc
        if kind == _CREATE:
            steps.append(BuilderStep(operation=operation, ref=ref))
        else:
            steps.append(BuilderStep(operation=operation, agent_ref=agent_ref))
    return steps


def builder_operation_data(step: BuilderStep) -> dict[str, Any]:
    """The persisted form: the validated operation, with `ref`/`agentRef` in
    place of the placeholder id. `parse_builder_operations` reads it back."""
    data = operation_data(step.operation)
    if step.agent_ref is not None:
        data.pop("agentId", None)
        data["agentRef"] = step.agent_ref
    if step.ref is not None:
        data["ref"] = step.ref
    return data


def resolve_step(
    data: Mapping[str, Any], refs: Mapping[str, uuid.UUID]
) -> tuple[dict[str, Any], str | None]:
    """One stored step made applicable: `agentRef` becomes the created agent's
    id. Returns the operation data and the `ref` this step defines (if any).

    The stored form never carries an `agentId`, so one found here is refused
    rather than applied: only a ref already created in this apply resolves."""
    out = dict(data)
    ref = out.pop("ref", None)
    agent_ref = out.pop("agentRef", None)
    if "agentId" in out:
        raise BuilderInvalid("agent_id_not_allowed")
    if out.get("type") == _CREATE:
        if agent_ref is not None:
            raise BuilderInvalid("invalid")
        return out, (str(ref) if ref is not None else None)
    if ref is not None:
        raise BuilderInvalid("invalid")
    created = refs.get(agent_ref) if isinstance(agent_ref, str) else None
    if created is None:
        raise BuilderInvalid("unknown_ref")
    out["agentId"] = str(created)
    return out, None
