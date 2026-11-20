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
        # Ruling 4: older than 6 steps → observed_step < step_no - MASK_AGE_STEPS.
        # Spec: longer than 2000 chars → mask when len(body) > MASK_MIN_CHARS.
        if (
            observed_step >= step_no - MASK_AGE_STEPS
            or len(body) <= MASK_MIN_CHARS
            or observed_step in protected_steps
        ):
            continue

        tool = message.name or "tool"
        n = len(body)
        head = body[:MASK_HEAD_CHARS]
        if n > SPILL_THRESHOLD_CHARS:
            filename = spill_filename(observed_step, tool)
            content = (
                f"[Result of {tool} at step {observed_step}, {n} chars, "
                f"masked to save context. First {MASK_HEAD_CHARS} chars:\n"
                f"{head}\n"
                f'… Full result: read_run_file("{filename}") — it was kept as a file.]'
            )
        else:
            filename = None
            content = (
                f"[Result of {tool} at step {observed_step}, {n} chars, "
                f"masked to save context. First {MASK_HEAD_CHARS} chars:\n"
                f"{head}"
            )
        outgoing[index] = replace(message, content=content)

        key = message.tool_call_id or f"step-{observed_step}-{tool}"
        masked[key] = MaskRef(
            step=observed_step,
            tool=tool,
            chars=n,
            filename=filename,
        )

    return outgoing, masked
