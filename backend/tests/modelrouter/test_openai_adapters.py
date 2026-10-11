from __future__ import annotations

import base64
from typing import Any

import httpx
import pytest

from oc8.modelrouter.adapters._openai_common import to_openai_messages
from oc8.modelrouter.adapters.openai import OpenAIAdapter
from oc8.modelrouter.adapters.openai_compatible import OpenAICompatibleAdapter
from oc8.modelrouter.trim import _cost, overflow_tokens, trim_for_overflow, trim_to_budget
from oc8.modelrouter.types import (
    CompletionRequest,
    ImagePart,
    ModelParams,
    NeutralMessage,
    NeutralTool,
    TextPart,
    ToolCall,
)

pytestmark = pytest.mark.asyncio


def _fake_response(url: str, *, tool_call: bool = False) -> httpx.Response:
    request = httpx.Request("POST", url)
    if tool_call:
        body: dict[str, Any] = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "ping", "arguments": '{"x": 1}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
    else:
        body = {
            "choices": [
                {"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }
    return httpx.Response(200, json=body, request=request)


async def test_openai_adapter_completes_and_hits_the_fixed_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_post(
        self: httpx.AsyncClient, url: str, json: Any = None, **kw: Any
    ) -> httpx.Response:
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = kw.get("headers")
        return _fake_response(url)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    adapter = OpenAIAdapter(api_key="sk-test")
    req = CompletionRequest(
        provider="openai",
        model="gpt-4o",
        messages=[NeutralMessage(role="user", content="hello")],
        params=ModelParams(temperature=0.1, max_tokens=100),
    )
    result = await adapter.complete(req)
    assert result.text == "hi"
    assert result.provider == "openai"
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["json"]["model"] == "gpt-4o"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"


async def test_openai_adapter_parses_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_post(
        self: httpx.AsyncClient, url: str, json: Any = None, **kw: Any
    ) -> httpx.Response:
        return _fake_response(url, tool_call=True)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    adapter = OpenAIAdapter(api_key="sk-test")
    req = CompletionRequest(
        provider="openai",
        model="gpt-4o",
        messages=[NeutralMessage(role="user", content="use the tool")],
        tools=[
            NeutralTool(
                name="ping", description="ping", parameters={"type": "object", "properties": {}}
            )
        ],
    )
    result = await adapter.complete(req)
    assert result.stop_reason == "tool_use"
    assert result.tool_calls == [ToolCall(id="call_1", name="ping", arguments={"x": 1})]


async def test_openai_compatible_adapter_respects_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_post(
        self: httpx.AsyncClient, url: str, json: Any = None, **kw: Any
    ) -> httpx.Response:
        captured["url"] = url
        captured["headers"] = kw.get("headers")
        return _fake_response(url)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    adapter = OpenAICompatibleAdapter(base_url="https://api.mistral.ai/v1", api_key="mk-test")
    req = CompletionRequest(
        provider="openai_compatible",
        model="mistral-large-latest",
        messages=[NeutralMessage(role="user", content="hello")],
    )
    result = await adapter.complete(req)
    assert result.provider == "openai_compatible"
    assert captured["url"] == "https://api.mistral.ai/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer mk-test"


async def test_openai_compatible_adapter_omits_auth_header_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_post(
        self: httpx.AsyncClient, url: str, json: Any = None, **kw: Any
    ) -> httpx.Response:
        captured["headers"] = kw.get("headers")
        return _fake_response(url)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    adapter = OpenAICompatibleAdapter(base_url="http://localhost:8000/v1")
    req = CompletionRequest(
        provider="openai_compatible",
        model="local-model",
        messages=[NeutralMessage(role="user", content="hi")],
    )
    await adapter.complete(req)
    assert "Authorization" not in captured["headers"]


def test_a_user_turn_after_a_tool_result_gets_a_bridging_assistant_turn() -> None:
    """A resumed run sends `tool` then `user` with nothing in between, and strict
    chat templates reject that outright.

    Live evidence (2026-07-27, opaas_ai:odoo-gpt through litellm/vLLM): the exact
    sequence below returns 400 "Unexpected role 'user' after role 'tool'", which
    is how an approved action silently never happened. The same sequence with one
    assistant turn in between returns 200. The bridge's content must be non-empty
    -- an empty assistant message is rejected as "Invalid assistant message".
    """
    bridged = to_openai_messages(
        [
            NeutralMessage(role="user", content="do it"),
            NeutralMessage(
                role="assistant",
                tool_calls=[ToolCall(id="call_1", name="create_record", arguments={})],
            ),
            NeutralMessage(role="tool", content="parked", tool_call_id="call_1"),
            NeutralMessage(role="user", content="approved, carry on"),
        ]
    )
    roles = [m["role"] for m in bridged]
    assert roles == ["user", "assistant", "tool", "assistant", "user"]
    assert bridged[3]["content"], "an empty bridge is rejected upstream"


def test_an_ordinary_transcript_gains_no_bridging_turn() -> None:
    """The bridge fires only on the interrupted shape. A healthy transcript --
    where the assistant answers its own tool result -- must go out untouched, so
    ordinary traffic to every OpenAI-compatible provider is unchanged."""
    ordinary = to_openai_messages(
        [
            NeutralMessage(role="user", content="do it"),
            NeutralMessage(
                role="assistant",
                tool_calls=[ToolCall(id="call_1", name="create_record", arguments={})],
            ),
            NeutralMessage(role="tool", content="done", tool_call_id="call_1"),
            NeutralMessage(role="assistant", content="created it"),
            NeutralMessage(role="user", content="thanks"),
        ]
    )
    assert [m["role"] for m in ordinary] == ["user", "assistant", "tool", "assistant", "user"]
    assert ordinary[3]["content"] == "created it"


async def test_system_messages_are_merged_and_hoisted_to_the_front() -> None:
    """Some OpenAI-compatible gateways reject any system message except one
    first message. oc8 can create multiple system notes before and during a run,
    so the adapter must present them as one leading system prompt.
    """
    sent = to_openai_messages(
        [
            NeutralMessage(role="system", content="base instructions"),
            NeutralMessage(role="user", content="do it"),
            NeutralMessage(
                role="assistant",
                tool_calls=[ToolCall(id="call_1", name="create_record", arguments={})],
            ),
            NeutralMessage(role="tool", content="parked", tool_call_id="call_1"),
            NeutralMessage(role="system", content="history was trimmed"),
            NeutralMessage(role="user", content="approved, carry on"),
        ]
    )
    assert [m["role"] for m in sent] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assert sent[0]["content"] == "base instructions\n\nhistory was trimmed"
    assert sent[4]["content"], "tool-to-user bridge must still be inserted after hoisting"


async def test_a_nameless_tool_call_never_reaches_the_provider() -> None:
    """A function with no name poisons a transcript permanently.

    Live, 2026-07-27: the model returned one tool call with an empty name, we
    stored it faithfully, and every later turn replayed it -- 400 "Function name
    was  but must be a-z…", twelve times in an hour, the run dead each time.
    Deterministic once it is in there, so nothing recovers on retry.

    Dropping the call alone would leave its tool RESULT behind, referring to a
    call that no longer exists -- invalid in a different way -- so the pair goes
    together.
    """
    sent = to_openai_messages(
        [
            NeutralMessage(role="user", content="do it"),
            NeutralMessage(
                role="assistant",
                tool_calls=[
                    ToolCall(id="call_ok", name="search_records", arguments={}),
                    ToolCall(id="call_bad", name="", arguments={}),
                ],
            ),
            NeutralMessage(role="tool", content="found 3", tool_call_id="call_ok"),
            NeutralMessage(role="tool", content="orphan", tool_call_id="call_bad"),
        ]
    )
    names = [c["function"]["name"] for msg in sent for c in msg.get("tool_calls", [])]
    assert names == ["search_records"], "the nameless call must be gone"
    tool_ids = [m["tool_call_id"] for m in sent if m["role"] == "tool"]
    assert tool_ids == ["call_ok"], "its result must go with it"


def test_to_openai_messages_builds_an_image_url_content_block() -> None:
    """Task 5: the OpenAI-compatible shape for an image is a content block
    that is a SIBLING of the text block inside `content`, `{"type":
    "image_url", "image_url": {"url": "data:<content_type>;base64,<data>"}}`
    -- different from Anthropic's `{"type": "image", "source": {...}}`."""
    msg = NeutralMessage(
        role="user",
        content=[
            TextPart(text="what's in this image?"),
            ImagePart(data=b"\x89PNG...", content_type="image/png"),
        ],
    )
    sent = to_openai_messages([msg])
    encoded = base64.b64encode(b"\x89PNG...").decode()
    assert sent[0]["role"] == "user"
    assert sent[0]["content"][0] == {"type": "text", "text": "what's in this image?"}
    assert sent[0]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{encoded}"},
    }


def test_a_plain_string_user_message_is_unaffected_by_the_image_helper() -> None:
    sent = to_openai_messages([NeutralMessage(role="user", content="hi there")])
    assert sent[0] == {"role": "user", "content": "hi there"}


def test_an_assistant_turn_with_only_image_parts_and_no_tool_calls_is_kept() -> None:
    """An assistant turn's `content` block list can be non-empty (an image)
    while carrying no text and no tool calls -- that must still count as
    "something to say", not get dropped by the empty-turn guard."""
    sent = to_openai_messages(
        [
            NeutralMessage(
                role="assistant",
                content=[ImagePart(data=b"\x89PNG...", content_type="image/png")],
            )
        ]
    )
    assert len(sent) == 1
    assert sent[0]["content"][0]["type"] == "image_url"


async def test_an_assistant_turn_left_with_nothing_is_dropped_entirely() -> None:
    """If the nameless call was the turn's ONLY content, what remains is an
    assistant message with neither text nor calls -- which the same endpoint
    rejects as an invalid assistant message."""
    sent = to_openai_messages(
        [
            NeutralMessage(role="user", content="do it"),
            NeutralMessage(role="assistant", tool_calls=[ToolCall(id="x", name="", arguments={})]),
            NeutralMessage(role="user", content="still there?"),
        ]
    )
    assert [m["role"] for m in sent] == ["user", "user"]


def _long(role: str, n: int, tag: str = "") -> NeutralMessage:
    return NeutralMessage(role=role, content=f"{tag}{'wort ' * n}")


async def test_a_transcript_that_would_overflow_is_trimmed_from_the_middle() -> None:
    """A run dies outright once its conversation passes the model's window: the
    provider answers with a NEGATIVE max_tokens and no retry can help, because
    the next attempt sends the same transcript (live, 2026-07-28: -7075).

    What may be dropped is the MIDDLE. The system message is the agent's whole
    instruction set and the last turns are what it is doing right now; losing
    either changes the answer rather than shortening the input.
    """
    messages = [
        NeutralMessage(role="system", content="Du bist Sina."),
        *[_long("user", 400, f"alt-{i} ") for i in range(20)],
        _long("user", 10, "aktuell "),
    ]
    kept = trim_to_budget(messages, budget_tokens=2000)

    assert kept[0].role == "system" and "Du bist Sina." in kept[0].content
    assert "aktuell" in kept[-1].content
    assert len(kept) < len(messages), "something has to give"


async def test_nothing_is_dropped_when_it_already_fits() -> None:
    """The common case must be untouched -- a trim that fires when it need not
    would quietly change what every agent sees."""
    messages = [
        NeutralMessage(role="system", content="Du bist Sina."),
        NeutralMessage(role="user", content="kurz"),
        NeutralMessage(role="assistant", content="auch kurz"),
    ]
    assert trim_to_budget(messages, budget_tokens=100_000) == messages


async def test_the_drop_is_announced_to_the_model() -> None:
    """Silently deleting turns would let the model contradict itself with total
    confidence -- it would have no way to know its own history is incomplete."""
    messages = [
        NeutralMessage(role="system", content="sys"),
        *[_long("user", 400, f"alt-{i} ") for i in range(20)],
        _long("user", 10, "aktuell "),
    ]
    kept = trim_to_budget(messages, budget_tokens=2000)
    assert any("gekürzt" in msg.content or "trimmed" in msg.content.lower() for msg in kept)


async def test_a_tool_result_never_outlives_its_call() -> None:
    """Dropping a call but keeping its result leaves a `tool` message referring
    to nothing, which providers reject -- the same shape the nameless-call guard
    exists for."""
    messages = [
        NeutralMessage(role="system", content="sys"),
        *[_long("user", 300, f"alt-{i} ") for i in range(15)],
        NeutralMessage(
            role="assistant", tool_calls=[ToolCall(id="c1", name="search", arguments={})]
        ),
        NeutralMessage(role="tool", content="ergebnis", tool_call_id="c1"),
        _long("user", 10, "aktuell "),
    ]
    kept = trim_to_budget(messages, budget_tokens=1200)
    call_ids = {tc.id for msg in kept for tc in msg.tool_calls}
    result_ids = {msg.tool_call_id for msg in kept if msg.role == "tool"}
    assert result_ids <= call_ids, "an orphaned tool result survived the trim"


async def test_the_deficit_is_read_out_of_the_providers_own_complaint() -> None:
    """No context window has to be configured anywhere: the provider states how
    far over the request was, and the endpoint we use publishes no window at all
    (checked -- /v1/models reports ids and nothing else)."""
    assert overflow_tokens("max_tokens must be at least 1, got -7075") == 7075
    assert overflow_tokens("some other 400") is None


async def test_a_transcript_with_nothing_left_to_drop_is_not_retried() -> None:
    """Retrying an identical request would turn one clear failure into a loop."""
    messages = [NeutralMessage(role="system", content="sys"), _long("user", 5, "jetzt ")]
    assert trim_for_overflow(messages, over_by=100_000, headroom=2048) is None


def test_cost_of_list_content_ignores_image_parts_and_does_not_crash() -> None:
    """`NeutralMessage.content` can be `str | list[ContentPart]` (Task 4's
    image-attachment support). The old `text = message.content or ""`
    followed by `text += ...` blew up with a `TypeError` the moment content
    was a non-empty list -- `or` binds `text` to the list itself, and a list
    has no `+=` against a string. Only `TextPart` entries should count
    toward this rough token estimate; an `ImagePart`'s bytes must not."""
    text_only = NeutralMessage(role="user", content="hello world")
    mixed = NeutralMessage(
        role="user",
        content=[
            TextPart(text="hello world"),
            ImagePart(data=b"\x89PNG" * 500, content_type="image/png"),
        ],
    )
    assert _cost(mixed) == _cost(text_only)


async def test_a_message_with_list_content_does_not_crash_the_budget_check() -> None:
    """`_cost` is called unconditionally on every message just to sum the
    total, even when the transcript already fits -- so this alone exercises
    the crash path from the docstring above without needing to force a trim."""
    messages = [
        NeutralMessage(role="system", content="sys"),
        NeutralMessage(
            role="user",
            content=[
                TextPart(text="what's in this image?"),
                ImagePart(data=b"\x89PNG...", content_type="image/png"),
            ],
        ),
    ]
    assert trim_to_budget(messages, budget_tokens=100_000) == messages


async def test_a_list_content_message_survives_an_actual_trim_without_crashing() -> None:
    """Same crash risk, but exercised inside the trimming loop itself (the
    per-message `cost = _cost(message)` call), not just the initial sum."""
    messages = [
        NeutralMessage(role="system", content="sys"),
        *[_long("user", 400, f"alt-{i} ") for i in range(20)],
        NeutralMessage(
            role="user",
            content=[
                TextPart(text="aktuell, what's in this image?"),
                ImagePart(data=b"\x89PNG...", content_type="image/png"),
            ],
        ),
    ]
    kept = trim_to_budget(messages, budget_tokens=2000)
    assert len(kept) < len(messages), "something has to give"
    assert isinstance(kept[-1].content, list)
    assert any(isinstance(p, ImagePart) for p in kept[-1].content)
