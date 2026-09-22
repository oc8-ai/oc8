"""A5 observation masking for model-bound transcript copies."""

from __future__ import annotations

import re
from dataclasses import replace

from oc8.agent.harness.stages.c_spill import SPILL_THRESHOLD_CHARS, spill_filename
from oc8.agent.harness.state import Ledger, MaskRef
from oc8.modelrouter import NeutralMessage

MASK_AGE_STEPS = 6
MASK_MIN_CHARS = 2_000
MASK_HEAD_CHARS = 400

_STAMP = re.compile(r"^(\[step (?P<step>\d+)/\d+ · [^\]]+\]) ?(?P<body>.*)$", re.DOTALL)


def mask_observations(
    messages: list[NeutralMessage], *, step_no: int, ledger: Ledger
) -> tuple[list[NeutralMessage], dict[str, MaskRef]]:
    """Mask old, large tool results in a new model-bound message list."""
    outgoing = list(messages)
    masked: dict[str, MaskRef] = {}
    protected_steps = {
        entity.last_read_step
        for key in ledger.writes_unverified
        if (entity := ledger.entities.get(key)) is not None
        and entity.last_read_step is not None
    }

    for index, message in enumerate(messages):
        if message.role != "tool" or not isinstance(message.content, str):
            continue
        match = _STAMP.match(message.content)
        if match is None:
            continue
        observed_step = int(match.group("step"))
        body = match.group("body")
        if (
            step_no - observed_step < MASK_AGE_STEPS
            or len(body) < MASK_MIN_CHARS
            or observed_step in protected_steps
        ):
            continue

        tool = message.name or "tool"
        filename = (
            spill_filename(observed_step, tool)
            if len(body) > SPILL_THRESHOLD_CHARS
            else None
        )
        notice = f"\n[… {len(body) - MASK_HEAD_CHARS} older result chars masked.]"
        if filename is not None:
            notice += f'\nUse read_run_file("{filename}") to retrieve the full result.'
        content = f"{match.group(1)} {body[:MASK_HEAD_CHARS]}{notice}"
        outgoing[index] = replace(message, content=content)

        key = message.tool_call_id or f"step-{observed_step}-{tool}"
        masked[key] = MaskRef(
            step=observed_step,
            tool=tool,
            chars=len(body),
            filename=filename,
        )

    return outgoing, masked
