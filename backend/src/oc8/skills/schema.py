"""The pinned `SkillVersion.definition` schema (tech-spec §6.5).

Parsing is tolerant on the way in -- the seeded rows predate this schema -- and
strict about what it exposes. A single malformed guardrail is dropped rather
than raised: a bad guardrail must not cost a run that is otherwise fine. Only a
definition with no usable instruction is rejected outright, because a skill
without one has nothing to contribute.

The `requires.tools` form pinned here is the FLAT one, `[{tool, rights}]` (a
bare string means `rights=["read"]`), because `authz.pdp.missing_skill_requirements`
already implements and tests it. The nested form in the technical specification
is the outlier and is corrected there, not here.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_RIGHTS: tuple[str, ...] = ("read",)


class SkillDefinitionError(ValueError):
    """The definition cannot be used as a skill."""


@dataclass(frozen=True)
class SkillRequirement:
    tool: str
    rights: tuple[str, ...]


@dataclass(frozen=True)
class Guardrail:
    type: str
    action: str
    metric: str
    gt: float
    then: str


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    requires_kind: str  # "read_of" | "tool_called" | "confirmation" | "manual"
    requires_value: str  # kind, tool name, "" for confirmation, "" for manual
    required: bool
    gates: tuple[str, ...]


@dataclass(frozen=True)
class SkillDefinition:
    schema_version: int
    slug: str
    version: str
    instruction: str
    requires_tools: tuple[SkillRequirement, ...]
    requires_kbs: tuple[str, ...]
    guardrails: tuple[Guardrail, ...]
    prose_guardrails: tuple[str, ...]
    #: "<capa_name>/<skill_subpath>" this skill's references/assets/scripts
    #: live under, or None when it has no on-disk directory to serve from --
    #: see capas.manifest.SkillTemplateSpec.reference_root, which this is
    #: derived from at materialise time (capas/materialise.py).
    reference_root: str | None = None
    steps: tuple[Step, ...] = ()
    #: True only while this skill is active. The run does not turn code mode
    #: on for every skill, and a task that must compact stays on the tool loop.
    code_mode: bool = False


def _parse_requires(requires: Any) -> tuple[str, str] | None:
    if not isinstance(requires, Mapping):
        return None

    allowed_keys = frozenset({"read_of", "tool_called", "confirmation", "manual"})
    variants: list[tuple[str, str]] = []

    for key, value in requires.items():
        if key not in allowed_keys:
            return None
        if key == "read_of":
            if not isinstance(value, str) or not value:
                return None
            variants.append(("read_of", value))
        elif key == "tool_called":
            if not isinstance(value, str) or not value:
                return None
            variants.append(("tool_called", value))
        elif key == "confirmation":
            if value is not True:
                return None
            variants.append(("confirmation", ""))
        elif key == "manual":
            if value is not True:
                return None
            variants.append(("manual", ""))

    if len(variants) != 1:
        return None
    return variants[0]


def _step(raw: Any) -> Step | None:
    if not isinstance(raw, Mapping):
        logger.warning("dropping malformed skill step: %r", raw)
        return None

    step_id = raw.get("id")
    title = raw.get("title")
    if not isinstance(step_id, str) or not step_id:
        logger.warning("dropping malformed skill step: %r", raw)
        return None
    if not isinstance(title, str) or not title:
        logger.warning("dropping malformed skill step: %r", raw)
        return None

    requires_parsed = _parse_requires(raw.get("requires"))
    if requires_parsed is None:
        logger.warning("dropping malformed skill step: %r", raw)
        return None
    requires_kind, requires_value = requires_parsed

    required_raw = raw.get("required", True)
    required = required_raw if isinstance(required_raw, bool) else True

    gates: list[str] = []
    gates_raw = raw.get("gates", ())
    if isinstance(gates_raw, Sequence) and not isinstance(gates_raw, str):
        for gate in gates_raw:
            if isinstance(gate, str) and gate:
                gates.append(gate)

    return Step(
        id=step_id,
        title=title,
        requires_kind=requires_kind,
        requires_value=requires_value,
        required=required,
        gates=tuple(gates),
    )


def _requirement(raw: Any) -> SkillRequirement | None:
    if isinstance(raw, str):
        return SkillRequirement(tool=raw, rights=DEFAULT_RIGHTS) if raw else None
    if isinstance(raw, Mapping):
        tool = raw.get("tool")
        if not isinstance(tool, str) or not tool:
            return None
        rights = (
            tuple(r for r in raw["rights"] if isinstance(r, str))
            if "rights" in raw
            and isinstance(raw["rights"], Sequence)
            and not isinstance(raw["rights"], str)
            else DEFAULT_RIGHTS
        )
        return SkillRequirement(tool=tool, rights=rights)
    return None


def _schema_version(raw: Any) -> int:
    try:
        return int(raw or 1)
    except (TypeError, ValueError):
        logger.warning("dropping malformed oc8_skill schema_version: %r", raw)
        return 1


def _guardrail(raw: Mapping[str, Any]) -> Guardrail | None:
    try:
        return Guardrail(
            type=str(raw["type"]),
            action=str(raw.get("action", "")),
            metric=str(raw.get("metric", "")),
            gt=float(raw["gt"]),
            then=str(raw["then"]),
        )
    except (KeyError, TypeError, ValueError):
        logger.warning("dropping malformed skill guardrail: %r", raw)
        return None


def parse_definition(data: Mapping[str, Any]) -> SkillDefinition:
    if not isinstance(data, Mapping):
        raise SkillDefinitionError("definition must be a mapping")
    instruction = data.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        raise SkillDefinitionError("definition has no usable 'instruction'")

    requires = data.get("requires")
    requires = requires if isinstance(requires, Mapping) else {}

    tools_raw = requires.get("tools")
    tools = (
        tuple(r for r in (_requirement(t) for t in tools_raw) if r is not None)
        if isinstance(tools_raw, Sequence) and not isinstance(tools_raw, str)
        else ()
    )

    kbs_raw = requires.get("kbs")
    kbs: tuple[str, ...] = ()
    if isinstance(kbs_raw, Sequence) and not isinstance(kbs_raw, str):
        collected: list[str] = []
        for k in kbs_raw:
            if isinstance(k, str) and k:
                collected.append(k)
            elif isinstance(k, Mapping) and isinstance(k.get("kb_ref"), str):
                collected.append(str(k["kb_ref"]))
        kbs = tuple(collected)

    structured: list[Guardrail] = []
    prose: list[str] = []
    guardrails_raw = data.get("guardrails")
    if isinstance(guardrails_raw, Sequence) and not isinstance(guardrails_raw, str):
        for g in guardrails_raw:
            if isinstance(g, str):
                if g.strip():
                    prose.append(g)
            elif isinstance(g, Mapping):
                parsed = _guardrail(g)
                if parsed is not None:
                    structured.append(parsed)
            else:
                logger.warning("dropping malformed skill guardrail: %r", g)

    reference_root_raw = data.get("reference_root")
    reference_root = (
        reference_root_raw
        if isinstance(reference_root_raw, str) and reference_root_raw
        else None
    )

    steps: tuple[Step, ...] = ()
    steps_raw = data.get("steps")
    if isinstance(steps_raw, Sequence) and not isinstance(steps_raw, str):
        parsed_steps: list[Step] = []
        for step_raw in steps_raw:
            parsed = _step(step_raw)
            if parsed is not None:
                parsed_steps.append(parsed)
        steps = tuple(parsed_steps)

    return SkillDefinition(
        schema_version=_schema_version(data.get("oc8_skill", 1)),
        slug=str(data.get("id", "")),
        version=str(data.get("version", "")),
        instruction=instruction.strip(),
        requires_tools=tools,
        requires_kbs=kbs,
        guardrails=tuple(structured),
        prose_guardrails=tuple(prose),
        reference_root=reference_root,
        steps=steps,
        code_mode=data.get("code_mode") is True,
    )
