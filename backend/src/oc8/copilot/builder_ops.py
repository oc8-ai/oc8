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


# -- The flat propose_agent shape ---------------------------------------------
#
# Small local models (qwen2.5:7b in the browser E2E) never produced a valid
# ordered `operations` list with refs. They fill a flat record reliably, so
# propose_agent also takes one agent's fields directly and this module expands
# them into the very operations a capable model would write. The expansion
# only rearranges what it was given: every value still goes through
# `parse_builder_operations` and the same selection and secret checks.

#: The one ref a flat proposal's operations use.
NEW_REF: Final = "$new"
#: What the flat shape takes; anything else is refused, never dropped.
FLAT_FIELDS: Final = frozenset(
    {
        "departmentId",
        "name",
        "roleTitle",
        "mission",
        "modelId",
        "skillIds",
        "schedule",
        "eventSource",
        "eventType",
        "taskText",
        "systems",
        "limits",
    }
)
#: A flat limit's own keys; `system` is the guardrail's `connectionName`.
_LIMIT_KEYS: Final = {"system": "connectionName"}


def _given(value: object) -> bool:
    """Small models send null, "" or [] for a field they mean to leave out."""
    return value not in (None, "", [], {})


def _listed(value: object) -> list[object]:
    """A list field; one bare string is read as a list of one."""
    if not _given(value):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return list(value)
    raise BuilderInvalid("invalid")


def _system_policy(frame: Mapping[str, Any] | None, system: str) -> dict[str, Any]:
    """The narrowing entry that lets the new agent use `system` as far as the
    department's frame allows -- the frame's own rights, never more, so it can
    not widen anything (`pdp.narrowing_within_frame`). A system the frame does
    not list is an agent-exclusive grant: reading on, writing off (a write
    stays something a person grants on the agent's page). Without a frame
    (unknown department) nothing is granted; the proposal is refused anyway."""
    tools = (frame or {}).get("tools")
    raw = tools.get(system) if isinstance(tools, dict) else None
    if frame is not None and system not in (tools if isinstance(tools, dict) else {}):
        return {"enabled": True, "read": True, "modify": False}
    policy = raw if isinstance(raw, dict) else {}
    return {
        "enabled": True,
        "read": bool(policy.get("read")),
        "modify": bool(policy.get("modify")),
    }


def flat_operations(
    fields: Mapping[str, Any], *, frame: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """The ordered builder operations one flat proposal stands for:
    agent.create (ref "$new"), then the model, each skill, the schedule and/or
    event trigger, the systems as one narrowing and each limit as a guardrail,
    every one with agentRef "$new". `frame` is the chosen department's frame;
    it only decides which rights a named system keeps."""
    unknown = set(fields) - FLAT_FIELDS
    if unknown:
        raise BuilderInvalid("unknown_field")
    create: dict[str, Any] = {"type": "agent.create", "ref": NEW_REF}
    for key in ("departmentId", "name", "roleTitle", "mission"):
        if key in fields and fields[key] is not None:
            create[key] = fields[key]
    ops: list[dict[str, Any]] = [create]

    def follow(op_type: str, /, **data: Any) -> None:
        ops.append({"type": op_type, "agentRef": NEW_REF, **data})

    if _given(fields.get("modelId")):
        follow("agent.model.switch", modelConfigId=fields["modelId"])
    for skill in _listed(fields.get("skillIds")):
        follow("agent.skill.assign", skillVersionId=skill)
    task = fields.get("taskText") if _given(fields.get("taskText")) else fields.get("mission")
    if _given(fields.get("schedule")):
        follow("trigger.create", kind="cron", cronExpression=fields["schedule"], taskText=task)
    if _given(fields.get("eventSource")) or _given(fields.get("eventType")):
        follow(
            "trigger.create",
            kind="event",
            eventSource=fields.get("eventSource"),
            eventType=fields.get("eventType"),
            taskText=task,
        )
    systems = _listed(fields.get("systems"))
    if systems:
        if not all(isinstance(s, str) for s in systems):
            raise BuilderInvalid("invalid", len(ops))
        tools = {str(s): _system_policy(frame, str(s)) for s in systems}
        follow("agent.narrowing.set", narrowing={"tools": tools})
    for limit in _listed(fields.get("limits")):
        if not isinstance(limit, dict):
            raise BuilderInvalid("invalid", len(ops))
        follow(
            "agent.guardrail.set",
            **{_LIMIT_KEYS.get(str(key), str(key)): value for key, value in limit.items()},
        )
    return ops


def proposal_operations(arguments: object, *, frame: Mapping[str, Any] | None = None) -> object:
    """The operations one propose_agent call carries, in either shape: the
    explicit `operations` list as given, or the flat fields expanded by
    `flat_operations`. Both, or neither, is refused. The result is still
    unvalidated -- `parse_builder_operations` judges it."""
    if not isinstance(arguments, Mapping):
        raise BuilderInvalid("invalid")
    flat = {key: value for key, value in arguments.items() if key != "operations"}
    if "operations" in arguments:
        if any(_given(value) for value in flat.values()):
            raise BuilderInvalid("mixed_shapes")
        return arguments["operations"]
    if not any(_given(value) for value in flat.values()):
        raise BuilderInvalid("missing_agent")
    return flat_operations(flat, frame=frame)


def flat_department_id(arguments: object) -> str | None:
    """The department a flat proposal names, to read its frame by."""
    if isinstance(arguments, Mapping) and "operations" not in arguments:
        value = arguments.get("departmentId")
        return value if isinstance(value, str) else None
    return None
