"""A model's reasoning, read the same way from every provider.

Each adapter has its own place for reasoning (a `reasoning_content` field,
inline `<think>` tags, Anthropic `thinking` blocks, Ollama's `thinking`, a
Responses API `reasoning` item). All of them must land in
`CompletionResult.reasoning` / `CompletionChunk.reasoning`, never in the answer
text, and a cut-off answer must read as `stop_reason == "length"`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from oc8.modelrouter.accumulate import accumulate_stream
from oc8.modelrouter.adapters._openai_common import parse_response
from oc8.modelrouter.adapters.anthropic import AnthropicAdapter, _to_anthropic_messages
from oc8.modelrouter.adapters.chatgpt_subscription import (
    _chunk_from_event,
    parse_responses_result,
)
from oc8.modelrouter.adapters.ollama import OllamaAdapter
from oc8.modelrouter.reasoning import (
    REASONING_STORE_LIMIT,
    REASONING_TRUNCATED_MARKER,
    ThinkTagSplitter,
    bound_reasoning,
    reasoning_field,
    reasoning_tokens_from,
    split_think_tags,
)
from oc8.modelrouter.streaming import stream_openai_sse
from oc8.modelrouter.types import (
    CompletionChunk,
    CompletionRequest,
    ModelParams,
    NeutralMessage,
    ToolCall,
)

# --- shared helpers ----------------------------------------------------------


def test_split_think_tags_moves_the_thought_out_of_the_answer() -> None:
    text, reasoning = split_think_tags("<think>plan the call</think>\n\nThe answer.")
    assert text == "The answer."
    assert reasoning == "plan the call"


def test_split_think_tags_leaves_plain_text_alone() -> None:
    assert split_think_tags("no tags here") == ("no tags here", "")


def test_an_unclosed_think_tag_is_all_reasoning() -> None:
    """A model that ran out of budget mid-thought: none of it is an answer."""
    text, reasoning = split_think_tags("<think>still thinking when cut")
    assert text == ""
    assert reasoning == "still thinking when cut"


def test_the_splitter_handles_a_tag_cut_across_chunks() -> None:
    splitter = ThinkTagSplitter()
    visible: list[str] = []
    thought: list[str] = []
    for piece in ("<thi", "nk>abc</th", "ink>Answer", " <", "done"):
        v, r = splitter.feed(piece)
        visible.append(v)
        thought.append(r)
    v, r = splitter.flush()
    visible.append(v)
    thought.append(r)
    assert "".join(visible) == "Answer <done"
    assert "".join(thought) == "abc"


def test_reasoning_content_wins_over_reasoning() -> None:
    assert reasoning_field({"reasoning_content": "a", "reasoning": "a"}) == "a"
    assert reasoning_field({"reasoning": "b"}) == "b"
    assert reasoning_field({"content": "x"}) == ""


def test_reasoning_tokens_unknown_is_none_not_zero() -> None:
    assert reasoning_tokens_from({}, "completion_tokens_details") is None
    assert reasoning_tokens_from(None, "completion_tokens_details") is None
    usage = {"completion_tokens_details": {"reasoning_tokens": 812}}
    assert reasoning_tokens_from(usage, "completion_tokens_details") == 812


def test_stored_reasoning_is_bounded_with_a_marker() -> None:
    bounded = bound_reasoning("x" * (REASONING_STORE_LIMIT + 10))
    assert bounded is not None
    assert len(bounded) == REASONING_STORE_LIMIT
    assert bounded.endswith(REASONING_TRUNCATED_MARKER)
    assert bound_reasoning("short") == "short"
    assert bound_reasoning("") is None


# --- OpenAI-compatible -------------------------------------------------------


def test_openai_response_reads_reasoning_content_and_reasoning_tokens() -> None:
    result = parse_response(
        {
            "choices": [
                {
                    "message": {"content": "Done.", "reasoning_content": "thinking..."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 50,
                "completion_tokens_details": {"reasoning_tokens": 40},
            },
        },
        provider="litellm",
        model="m",
    )
    assert result.text == "Done."
    assert result.reasoning == "thinking..."
    assert result.usage.reasoning_tokens == 40
    assert result.stop_reason == "stop"


def test_openai_response_strips_inline_think_tags_and_reports_length() -> None:
    result = parse_response(
        {
            "choices": [
                {"message": {"content": "<think>all budget spent here"}, "finish_reason": "length"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1536},
        },
        provider="litellm",
        model="m",
    )
    assert result.text == ""
    assert result.reasoning == "all budget spent here"
    assert result.stop_reason == "length"
    assert result.usage.reasoning_tokens is None


def _sse(*lines: str) -> bytes:
    return "".join(f"data: {line}\n\n" for line in lines).encode()


def _client(body: bytes) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_openai_stream_separates_reasoning_deltas_and_think_tags() -> None:
    body = _sse(
        '{"choices":[{"delta":{"reasoning_content":"first "}}]}',
        '{"choices":[{"delta":{"content":"<thi"}}]}',
        '{"choices":[{"delta":{"content":"nk>second</think>Hel"}}]}',
        '{"choices":[{"delta":{"content":"lo"},"finish_reason":"stop"}]}',
        '{"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":9,'
        '"completion_tokens_details":{"reasoning_tokens":6}}}',
        "[DONE]",
    )
    async with _client(body) as c:
        chunks = [
            ch async for ch in stream_openai_sse("http://x/v1/chat/completions", {}, {}, client=c)
        ]
    assert "".join(ch.text for ch in chunks) == "Hello"
    assert "".join(ch.reasoning for ch in chunks) == "first second"
    usage = next(ch.usage for ch in chunks if ch.usage is not None)
    assert usage.reasoning_tokens == 6


@pytest.mark.asyncio
async def test_accumulate_stream_assembles_reasoning_and_calls_back() -> None:
    async def chunks() -> AsyncIterator[CompletionChunk]:
        yield CompletionChunk(reasoning="a ")
        yield CompletionChunk(reasoning="b")
        yield CompletionChunk(text="answer", stop_reason="stop")

    seen: list[str] = []

    async def on_reasoning(text: str) -> None:
        seen.append(text)

    result = await accumulate_stream(chunks(), on_reasoning=on_reasoning)
    assert result.text == "answer"
    assert result.reasoning == "a b"
    assert seen == ["a ", "b"]


@pytest.mark.asyncio
async def test_accumulate_stream_reports_no_reasoning_as_none() -> None:
    async def chunks() -> AsyncIterator[CompletionChunk]:
        yield CompletionChunk(text="answer", stop_reason="stop")

    result = await accumulate_stream(chunks())
    assert result.reasoning is None


# --- fake streaming client for adapters that open their own --------------------


class _FakeStream:
    def __init__(self, body: bytes, content_type: str) -> None:
        self._resp = httpx.Response(
            200,
            content=body,
            headers={"content-type": content_type},
            request=httpx.Request("POST", "http://provider"),
        )

    async def __aenter__(self) -> httpx.Response:
        return self._resp

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _patch_stream(monkeypatch: pytest.MonkeyPatch, body: bytes, content_type: str) -> None:
    def fake_stream(self: httpx.AsyncClient, method: str, url: str, **kw: Any) -> _FakeStream:
        return _FakeStream(body, content_type)

    monkeypatch.setattr(httpx.AsyncClient, "stream", fake_stream)


# --- Anthropic ---------------------------------------------------------------


def _anthropic_req(messages: list[NeutralMessage] | None = None) -> CompletionRequest:
    return CompletionRequest(
        provider="anthropic",
        model="claude-sonnet-4-5",
        messages=messages or [NeutralMessage(role="user", content="hi")],
        params=ModelParams(temperature=0.3, max_tokens=64),
    )


@pytest.mark.asyncio
async def test_anthropic_stream_yields_thinking_and_the_signed_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        ("message_start", {"message": {"usage": {"input_tokens": 5}}}),
        (
            "content_block_start",
            {"index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        ),
        (
            "content_block_delta",
            {"index": 0, "delta": {"type": "thinking_delta", "thinking": "Let me "}},
        ),
        (
            "content_block_delta",
            {"index": 0, "delta": {"type": "thinking_delta", "thinking": "think."}},
        ),
        (
            "content_block_delta",
            {"index": 0, "delta": {"type": "signature_delta", "signature": "sig=="}},
        ),
        ("content_block_stop", {"index": 0}),
        ("content_block_start", {"index": 1, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"index": 1, "delta": {"type": "text_delta", "text": "Hi"}}),
        ("content_block_stop", {"index": 1}),
        ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}}),
    ]
    body = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()
    _patch_stream(monkeypatch, body, "text/event-stream")

    adapter = AnthropicAdapter(api_key="k")
    result = await accumulate_stream(adapter.stream(_anthropic_req()))
    assert result.text == "Hi"
    assert result.reasoning == "Let me think."
    assert result.replay_blocks == [
        {"type": "thinking", "thinking": "Let me think.", "signature": "sig=="}
    ]


def test_anthropic_replays_thinking_only_for_the_open_tool_exchange() -> None:
    thinking = {"type": "thinking", "thinking": "t", "signature": "s"}
    call = ToolCall(id="c1", name="lookup", arguments={})
    old_turn = NeutralMessage(
        role="assistant", content="", tool_calls=[call], replay_blocks=[thinking]
    )
    messages = [
        NeutralMessage(role="user", content="first"),
        old_turn,
        NeutralMessage(role="tool", content="r", tool_call_id="c1", name="lookup"),
        NeutralMessage(role="user", content="second"),
        NeutralMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c2", name="lookup", arguments={})],
            replay_blocks=[thinking],
        ),
        NeutralMessage(role="tool", content="r", tool_call_id="c2", name="lookup"),
    ]
    _, out = _to_anthropic_messages(messages)
    assistant_turns = [m for m in out if m["role"] == "assistant"]
    # The finished exchange before "second" is sent without its reasoning ...
    assert all(b.get("type") != "thinking" for b in assistant_turns[0]["content"])
    # ... the open one carries the signed block first, verbatim.
    assert assistant_turns[1]["content"][0] == thinking


# --- Ollama ------------------------------------------------------------------


def _ollama_req() -> CompletionRequest:
    return CompletionRequest(
        provider="ollama",
        model="qwen3",
        messages=[NeutralMessage(role="user", content="hi")],
        params=ModelParams(temperature=0.3, max_tokens=64),
    )


@pytest.mark.asyncio
async def test_ollama_stream_reads_thinking_and_a_length_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = [
        {"message": {"role": "assistant", "content": "", "thinking": "hmm "}},
        {"message": {"role": "assistant", "content": "<think>more"}},
        {
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "done_reason": "length",
            "prompt_eval_count": 4,
            "eval_count": 64,
        },
    ]
    body = ("\n".join(json.dumps(line) for line in lines) + "\n").encode()
    _patch_stream(monkeypatch, body, "application/x-ndjson")

    result = await accumulate_stream(OllamaAdapter("http://ollama").stream(_ollama_req()))
    assert result.text == ""
    assert result.reasoning == "hmm more"
    assert result.stop_reason == "length"


# --- ChatGPT subscription (Responses API) -------------------------------------


def test_responses_result_reads_reasoning_items_and_reasoning_tokens() -> None:
    result = parse_responses_result(
        {
            "status": "completed",
            "output": [
                {
                    "type": "reasoning",
                    "summary": [
                        {"type": "summary_text", "text": "Plan A."},
                        {"type": "summary_text", "text": "Plan B."},
                    ],
                },
                {"type": "message", "content": [{"type": "output_text", "text": "Done."}]},
            ],
            "usage": {
                "input_tokens": 3,
                "output_tokens": 20,
                "output_tokens_details": {"reasoning_tokens": 15},
            },
        },
        provider="openai_chatgpt",
        model="gpt-5",
    )
    assert result.text == "Done."
    assert result.reasoning == "Plan A.\n\nPlan B."
    assert result.usage.reasoning_tokens == 15
    assert result.stop_reason == "stop"


def test_an_incomplete_response_cut_by_its_cap_is_length() -> None:
    result = parse_responses_result(
        {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"type": "reasoning", "summary": []}],
            "usage": {"input_tokens": 3, "output_tokens": 64},
        },
        provider="openai_chatgpt",
        model="gpt-5",
    )
    assert result.text == ""
    assert result.reasoning is None
    assert result.stop_reason == "length"


def test_responses_stream_events_carry_reasoning_and_the_incomplete_stop() -> None:
    summary = _chunk_from_event(
        {"type": "response.reasoning_summary_text.delta", "delta": "Plan"}, saw_tool_call=False
    )
    assert summary is not None and summary.reasoning == "Plan"
    full = _chunk_from_event(
        {"type": "response.reasoning_text.delta", "delta": "raw"}, saw_tool_call=False
    )
    assert full is not None and full.reasoning == "raw"
    end = _chunk_from_event(
        {
            "type": "response.incomplete",
            "response": {
                "incomplete_details": {"reason": "max_output_tokens"},
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        },
        saw_tool_call=False,
    )
    assert end is not None
    assert end.stop_reason == "length"
    assert end.usage is not None and end.usage.tokens_out == 2
