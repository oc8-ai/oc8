"""Decide when to compact and rebuild a transcript around a checkpoint."""

from __future__ import annotations

from oc8.agent.harness.caps import ModelCaps
from oc8.agent.harness.state import HarnessState
from oc8.modelrouter.types import NeutralMessage, TextPart

CHECKPOINT_PREFIX = (
    "Context checkpoint: another pass of you produced this summary; continue "
    "the task directly, do not acknowledge it, and answer the newest request, "
    "not an older one.\n\n"
)
_LEDGER_PREFIX = "# Working state (supersedes earlier working-state blocks)"


def should_compact(state: HarnessState, caps: ModelCaps) -> bool:
    """Return whether the previous prompt crossed 80% and the gap is open."""
    return (
        state.last_prompt_tokens > int(0.8 * caps.context_window_tokens)
        and state.step_no - state.last_compacted_step >= 5
    )


def already_compacted_this_step(state: HarnessState) -> bool:
    """True after this step already paid for a summary.

    A context overflow on that same step must propagate. Another summary
    would be a second full model call on a transcript that was just rebuilt,
    and the gap rule allows one compaction per five steps.
    """
    return state.last_compacted_step == state.step_no


def _text(message: NeutralMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return "".join(part.text for part in message.content if isinstance(part, TextPart))


def prompt_token_fallback(messages: list[NeutralMessage]) -> int:
    """Estimate prompt tokens from joined message text without a tokenizer."""
    return len("".join(_text(message) for message in messages)) // 4


def _prefix_end(messages: list[NeutralMessage]) -> int:
    for index, message in enumerate(messages):
        if message.role == "user" and _text(message).startswith("# Run context"):
            return index + 1
    index = 0
    while index < len(messages) and messages[index].role == "system":
        index += 1
    return index


def _recent_tail(messages: list[NeutralMessage]) -> list[NeutralMessage]:
    candidates = [
        message
        for message in messages
        if not _text(message).startswith(_LEDGER_PREFIX)
    ]
    if not candidates:
        return []

    target_chars = max(1, (sum(len(_text(message)) for message in candidates) + 4) // 5)
    kept_chars = 0
    start = len(candidates)
    while start > 0 and kept_chars < target_chars:
        start -= 1
        kept_chars += len(_text(candidates[start]))

    if candidates[start].role == "tool":
        while start > 0 and candidates[start - 1].role == "tool":
            start -= 1
        if start > 0 and candidates[start - 1].role == "assistant":
            start -= 1
    return candidates[start:]


def rebuild_transcript(
    messages: list[NeutralMessage],
    *,
    summary: str,
    ledger_block: str,
    skill_blocks: list[str],
) -> list[NeutralMessage]:
    """Keep stable context, a summary checkpoint, recent turns, and skills."""
    prefix_end = _prefix_end(messages)
    prefix = list(messages[:prefix_end])
    tail = _recent_tail(messages[prefix_end:])
    return [
        *prefix,
        NeutralMessage(role="user", content=CHECKPOINT_PREFIX + summary),
        NeutralMessage(role="user", content=ledger_block),
        *tail,
        *(NeutralMessage(role="user", content=block) for block in skill_blocks),
    ]
