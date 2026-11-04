"""A model's reasoning, read the same way from every provider.

Providers disagree on where reasoning lives: a `reasoning_content` or
`reasoning` field next to the answer (OpenAI-compatible servers such as
LiteLLM, vLLM, OpenRouter and Ollama), `<think>...</think>` written inline
into the answer (several open-weight models), Anthropic `thinking` blocks, or
a Responses API `reasoning` item. Adapters translate each of those into
`CompletionResult.reasoning` / `CompletionChunk.reasoning`; this module holds
the parts more than one adapter needs.

Reasoning is for display only. Nothing here feeds it back into a later call.
"""

from __future__ import annotations

from typing import Any

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

#: Upper bound on the reasoning stored per step. Reasoning can be far longer
#: than the answer, and it lands in the run record that every run view loads.
REASONING_STORE_LIMIT = 64 * 1024
REASONING_TRUNCATED_MARKER = "\n\n[... reasoning truncated ...]"


def reasoning_field(message: dict[str, Any]) -> str:
    """The reasoning text of an OpenAI-shaped message or stream delta, or "".

    `reasoning_content` (DeepSeek, vLLM, LiteLLM) wins over `reasoning`
    (OpenRouter, Ollama's OpenAI endpoint) when both are present, because a
    gateway that sets both mirrors the same text into each.
    """
    for key in ("reasoning_content", "reasoning"):
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def reasoning_tokens_from(usage: dict[str, Any] | None, *details_keys: str) -> int | None:
    """`usage[<details>].reasoning_tokens` for the first details key present.

    None when the provider did not report it -- "unknown" is not zero.
    """
    if not isinstance(usage, dict):
        return None
    for key in details_keys:
        details = usage.get(key)
        if isinstance(details, dict) and details.get("reasoning_tokens") is not None:
            try:
                return int(details["reasoning_tokens"])
            except (TypeError, ValueError):
                return None
    return None


def split_think_tags(text: str) -> tuple[str, str]:
    """`text` with every `<think>...</think>` span removed, plus what they held.

    An unclosed `<think>` runs to the end: a model that ran out of budget
    mid-thought leaves exactly that, and showing it as the answer would be
    wrong twice over.
    """
    if THINK_OPEN not in text:
        return text, ""
    splitter = ThinkTagSplitter()
    visible, reasoning = splitter.feed(text)
    tail_visible, tail_reasoning = splitter.flush()
    # "<think>…</think>\n\nAnswer": the blank line belonged to the tag.
    return (visible + tail_visible).lstrip(), (reasoning + tail_reasoning).strip()


class ThinkTagSplitter:
    """Streaming version of `split_think_tags`.

    A tag can be cut across two chunks (`"<thi"` + `"nk>"`), so a suffix that
    could still become a tag is held back until the next chunk decides it.
    """

    def __init__(self) -> None:
        self._inside = False
        self._pending = ""

    def feed(self, text: str) -> tuple[str, str]:
        """Return the (visible, reasoning) text this fragment settles."""
        buffer = self._pending + text
        self._pending = ""
        visible: list[str] = []
        reasoning: list[str] = []
        while buffer:
            tag = THINK_CLOSE if self._inside else THINK_OPEN
            index = buffer.find(tag)
            if index >= 0:
                (reasoning if self._inside else visible).append(buffer[:index])
                buffer = buffer[index + len(tag) :]
                self._inside = not self._inside
                continue
            keep = _partial_tag_suffix(buffer, tag)
            settled = buffer[: len(buffer) - keep]
            (reasoning if self._inside else visible).append(settled)
            self._pending = buffer[len(buffer) - keep :]
            break
        return "".join(visible), "".join(reasoning)

    def flush(self) -> tuple[str, str]:
        """Whatever was held back, once the stream has ended."""
        pending, self._pending = self._pending, ""
        return ("", pending) if self._inside else (pending, "")


def _partial_tag_suffix(text: str, tag: str) -> int:
    """Length of the longest suffix of `text` that is a proper prefix of `tag`."""
    for size in range(min(len(tag) - 1, len(text)), 0, -1):
        if tag.startswith(text[-size:]):
            return size
    return 0


def bound_reasoning(text: str | None) -> str | None:
    """Reasoning cut to `REASONING_STORE_LIMIT` characters, with a marker."""
    if not text:
        return None
    if len(text) <= REASONING_STORE_LIMIT:
        return text
    return text[: REASONING_STORE_LIMIT - len(REASONING_TRUNCATED_MARKER)] + (
        REASONING_TRUNCATED_MARKER
    )
