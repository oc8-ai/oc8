"""NeutralMessage.cache_breakpoint on the Anthropic adapter: the marked
system block and the marked message's last content block carry
`cache_control`, at most four markers per request, none without caching."""

from __future__ import annotations

import json
from typing import Any

from oc8.modelrouter.adapters.anthropic import _build_payload
from oc8.modelrouter.types import (
    CompletionRequest,
    ImagePart,
    ModelParams,
    NeutralMessage,
    TextPart,
)

EPHEMERAL = {"type": "ephemeral"}


def _req(messages: list[NeutralMessage]) -> CompletionRequest:
    return CompletionRequest(
        provider="anthropic",
        model="claude-sonnet-4-5",
        messages=messages,
        params=ModelParams(max_tokens=64),
    )


def _chat() -> list[NeutralMessage]:
    return [
        NeutralMessage(role="system", content="rules"),
        NeutralMessage(role="system", content="catalog", cache_breakpoint=True),
        NeutralMessage(role="user", content="old question"),
        NeutralMessage(role="assistant", content="old answer", cache_breakpoint=True),
        NeutralMessage(role="user", content="new turn"),
    ]


def _markers(payload: dict[str, Any]) -> int:
    return json.dumps(payload).count('"cache_control"')


def test_anthropic_puts_cache_control_on_marked_blocks() -> None:
    payload = _build_payload(_req(_chat()), prompt_caching=True)

    assert payload["cache_control"] == EPHEMERAL
    assert payload["system"] == [
        {"type": "text", "text": "rules"},
        {"type": "text", "text": "catalog", "cache_control": EPHEMERAL},
    ]
    old_answer = payload["messages"][1]
    assert old_answer["content"] == [
        {"type": "text", "text": "old answer", "cache_control": EPHEMERAL}
    ]
    assert payload["messages"][0]["content"] == "old question"
    assert payload["messages"][2]["content"] == "new turn"


def test_marker_lands_on_the_last_block_of_a_multipart_message() -> None:
    parts = [TextPart(text="look"), ImagePart(data=b"x", content_type="image/png")]
    messages = [
        NeutralMessage(role="user", content=parts, cache_breakpoint=True),
        NeutralMessage(role="user", content="next"),
    ]
    content = _build_payload(_req(messages), prompt_caching=True)["messages"][0]["content"]
    assert "cache_control" not in content[0]
    assert content[-1]["cache_control"] == EPHEMERAL


def test_no_marker_without_prompt_caching() -> None:
    payload = _build_payload(_req(_chat()), prompt_caching=False)
    assert _markers(payload) == 0
    assert payload["system"] == "rules\ncatalog"


def test_unmarked_request_keeps_the_plain_system_string() -> None:
    messages = [
        NeutralMessage(role="system", content="a"),
        NeutralMessage(role="system", content="b"),
        NeutralMessage(role="user", content="hi"),
    ]
    assert _build_payload(_req(messages), prompt_caching=True)["system"] == "a\nb"


def test_at_most_four_cache_markers() -> None:
    messages = [
        NeutralMessage(
            role="user" if i % 2 == 0 else "assistant", content=f"m{i}", cache_breakpoint=True
        )
        for i in range(8)
    ]
    payload = _build_payload(_req(messages), prompt_caching=True)
    assert _markers(payload) == 4
    marked = [
        i
        for i, msg in enumerate(payload["messages"])
        if "cache_control" in json.dumps(msg["content"])
    ]
    assert marked == [5, 6, 7]
