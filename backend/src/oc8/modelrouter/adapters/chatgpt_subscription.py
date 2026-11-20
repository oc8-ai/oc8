# backend/src/oc8/modelrouter/adapters/chatgpt_subscription.py
"""Adapter for OpenAI's **Responses API**, as used by ChatGPT-subscription-backed
model connections (design: ChatGPT subscription auth, Task 7).

Deliberately separate from `OpenAICompatibleAdapter`: CHATGPT_BACKEND_BASE_URL
speaks the Responses API *only* -- Codex removed `wire_api = "chat"` support
entirely (see `oc8.oauth.openai_chatgpt_params`'s module docstring, quirk 9),
so `/chat/completions` is not available on this backend and none of
`_openai_common.py`'s translation transfers. The wire format differs in every
part that matters: a flat `input` array of typed items instead of `messages`,
tool calls and their results as *sibling* items rather than nesting, flat
function tools, an `output` array instead of `choices[].message`, and
`input_tokens`/`output_tokens` instead of `prompt_tokens`/`completion_tokens`.

Sources
-------
The Responses API is OpenAI's own current, public API. Every field below was
read from OpenAI's machine-readable spec, not from prose:

  - openai/openai-openapi, `openapi.yaml` (master, read 2026-08-26). Schemas:
    `CreateResponse` (:36274), `ResponseProperties` (:59577),
    `ModelResponseProperties` (:44539), `Response` (:57304, with a full
    verbatim `example:`), `ResponseUsage` (:60331), `OutputMessage` (:45301),
    `OutputTextContent` (:67893), `FunctionToolCall` (:40661),
    `FunctionToolCallOutput`, `FunctionTool` (:68593), `EasyInputMessage`,
    `InputItem`, `InputParam`. (`platform.openai.com/docs/api-reference/
    responses` serves the same content but 403s automated fetches; the
    OpenAPI document is the same material in a form that can be cited.)

Cross-checked against Codex's own request builder and SSE reader, at the same
commit Task 1 read (openai/codex @ 7625bd56657da7ce6d96b6d27e983e568757cdbc),
because Codex is the only public client proven against *this* base URL:

  - codex-rs/codex-api/src/endpoint/responses.rs:42 -- the endpoint path is
    the literal `"/responses"`, appended to the provider base URL by
    codex-rs/codex-api/src/provider.rs:53 `url_for_path` (plain
    trim-slash-and-join). Hence `{CHATGPT_BACKEND_BASE_URL}/responses` =
    `https://chatgpt.com/backend-api/codex/responses`.
  - codex-rs/codex-api/src/common.rs:273 `struct ResponsesApiRequest` -- the
    exact field set Codex serialises.
  - codex-rs/core/src/client.rs:889 `build_responses_request` -- notably
    `store: false`, `tool_choice: "auto"`, and **no `temperature` and no
    `max_output_tokens`**.
  - codex-rs/protocol/src/models.rs:961 `enum ResponseItem`
    (`#[serde(tag = "type", rename_all = "snake_case")]`) and :851
    `enum ContentItem` -- the input item and content-part shapes.
  - codex-rs/tools/src/responses_api.rs:32 `struct ResponsesApiTool` and
    codex-rs/tools/src/tool_spec.rs:22 `enum ToolSpec` (`#[serde(tag =
    "type")]`, `#[serde(rename = "function")]`) -- the flat tool object.
  - codex-rs/codex-api/src/sse/responses.rs:348 `process_responses_event` --
    the event names, and which ones Codex acts on.
  - codex-rs/model-provider/src/auth.rs:106 -- the header is spelled
    `ChatGPT-Account-ID`.

What that adds up to
--------------------
1. **Request.** `POST {base}/responses` with
   `{"model", "instructions"?, "input": [...], "tools"?, "tool_choice"?,
   "store": false, "stream": bool}`.
2. **`input` items** are typed and flat (`{"type": ...}` discriminator):
   - `{"type": "message", "role": "user"|"assistant", "content": [...]}` --
     and the content parts are asymmetric: what goes IN is
     `{"type": "input_text", "text": ...}`, a previous assistant turn is
     `{"type": "output_text", "text": ...}` (ContentItem, models.rs:851).
   - `{"type": "function_call", "call_id", "name", "arguments"}` where
     `arguments` is a JSON **string**, not an object.
   - `{"type": "function_call_output", "call_id", "output"}` where `output`
     is a string. Paired to the call by `call_id` -- NOT by the item's own
     `id`, which is why `ToolCall.id` carries `call_id` here.
   A system turn has no item type of its own in this mapping: system messages
   are joined into the top-level `instructions` field, which is exactly the
   slot Codex puts its base instructions in. That hoists a mid-transcript
   system note (oc8's own trim notice is one) to the front -- harmless here,
   because `oc8.modelrouter.trim` already collects every system turn into the
   head for exactly the same reason.
3. **Tools** are flat: `{"type": "function", "name", "description",
   "strict", "parameters"}` -- no `{"function": {...}}` wrapper. `strict` is
   a required field of `FunctionTool`; it is sent as `false` because oc8's
   tool schemas are ordinary JSON Schema and strict mode additionally
   requires every property to be listed in `required`.
4. **`ModelParams` is deliberately NOT forwarded.** `temperature` is rejected
   outright by the reasoning models this backend serves, and
   `max_output_tokens` counts invisible reasoning tokens against its cap, so
   forwarding a 1024 default would routinely return an empty `incomplete`
   response. Codex sends neither (client.rs:889). This is a real, known
   limitation of this provider rather than an oversight.
5. **Non-streaming response**: `{"id", "status", "output": [...], "usage":
   {"input_tokens", "output_tokens", "total_tokens", ...}}`. Text lives in
   `output[].content[]` where `type == "output_text"`; a tool call is its own
   `output[]` item with `type == "function_call"`. `output` may also hold
   `reasoning` items; their `summary[]` (`summary_text`) and `content[]`
   (`reasoning_text`) parts become `CompletionResult.reasoning`, for display
   only. `usage.output_tokens_details.reasoning_tokens` is the reasoning
   share of `output_tokens`.
6. **Streaming** is SSE framed as `event: <type>` + `data: <json>`, and every
   payload repeats its own `type`, so only the `data:` lines need reading.
   There is no `[DONE]` sentinel -- the stream simply ends after
   `response.completed` (sse/responses.rs has no `[DONE]` branch at all).
   Events used here:
   - `response.output_text.delta` -> `{"delta": "..."}` (text).
   - `response.output_item.done` -> `{"output_index", "item"}`; when the item
     is a `function_call` it is COMPLETE (`call_id`, `name`, and the whole
     `arguments` string). `response.function_call_arguments.delta` carries
     neither `call_id` nor `name`, so its fragments cannot be turned into a
     dispatchable call on their own -- Codex ignores them
     (sse/responses.rs:501) and reads the finished item instead, and so does
     this adapter.
   - `response.completed` -> `{"response": {... "usage": {...}}}`: the only
     place a stream reports usage. Missing it bills every streamed
     subscription run at zero.
   - `response.reasoning_summary_text.delta` and
     `response.reasoning_text.delta` -> `{"delta": "..."}` (reasoning).
   - `response.failed` -> `{"response": {"error": {"code", "message"}}}`.
   - `response.incomplete` -> like `response.completed`, with
     `response.incomplete_details.reason == "max_output_tokens"` when the
     answer was cut off.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from oc8.modelrouter.http_errors import araise_for_status_with_body, raise_for_status_with_body
from oc8.modelrouter.levels import EFFORT_FOR_LEVEL
from oc8.modelrouter.reasoning import reasoning_tokens_from
from oc8.modelrouter.types import (
    CompletionChunk,
    CompletionRequest,
    CompletionResult,
    ImagePart,
    NeutralMessage,
    NeutralTool,
    TextPart,
    ToolCall,
    ToolCallDelta,
    Usage,
)

logger = logging.getLogger(__name__)

#: codex-rs sends this on every request to the ChatGPT backend; see
#: oc8.oauth.openai_chatgpt_params's docstring, quirk 8.
_ORIGINATOR = "codex_cli_rs"

_TIMEOUT = 180.0


def to_responses_input(messages: list[NeutralMessage]) -> tuple[str, list[dict[str, Any]]]:
    """Neutral transcript -> `(instructions, input)`.

    System turns are hoisted into `instructions` (the Responses API's own slot
    for them); everything else becomes a typed item, in order.
    """
    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    # A call with no name cannot be dispatched by anyone and providers reject
    # the whole request over it. One such call poisons a transcript for good --
    # it is replayed on every later turn -- so it is dropped here, where an
    # ALREADY-poisoned transcript can still be continued. Same guard, same
    # reason as `_openai_common.to_openai_messages`.
    dropped_call_ids: set[str] = set()

    for msg in messages:
        if msg.role == "system":
            if msg.content:
                # System turns never carry images in this design -- they are
                # hoisted into the plain-text `instructions` field, which has
                # no content-part shape of its own. Same assumption as
                # `oc8.modelrouter.adapters.anthropic._to_anthropic_messages`.
                if isinstance(msg.content, str):
                    instructions.append(msg.content)
                else:
                    instructions.append(
                        "\n".join(p.text for p in msg.content if isinstance(p, TextPart))
                    )
        elif msg.role == "user":
            items.append(_message_item("user", "input_text", msg.content))
        elif msg.role == "assistant":
            if msg.content:
                items.append(_message_item("assistant", "output_text", msg.content))
            for call in msg.tool_calls:
                if not call.name.strip():
                    logger.warning("dropping a tool call with no name (id=%r)", call.id)
                    dropped_call_ids.add(call.id)
                    continue
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        # The Responses API wants the arguments as a JSON
                        # string, not an object (FunctionToolCall.arguments).
                        "arguments": json.dumps(call.arguments),
                    }
                )
        elif msg.role == "tool":
            # A result whose call was dropped now refers to nothing, which the
            # provider rejects in its own right -- the pair goes together.
            if (msg.tool_call_id or "") in dropped_call_ids:
                continue
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": msg.tool_call_id or "",
                    "output": msg.content,
                }
            )
    return "\n\n".join(instructions), items


def _message_item(role: str, part_type: str, content: str | list[Any]) -> dict[str, Any]:
    """A neutral message's content -> one Responses API `message` input item.

    The plain-`str` case keeps the original single-`input_text`/`output_text`
    item shape unchanged. A `list[ContentPart]` becomes a list of typed
    content parts in order: `TextPart` -> `{"type": part_type, "text": ...}`
    (so a user turn's text stays `input_text` and an assistant turn's stays
    `output_text`), and `ImagePart` -> `{"type": "input_image", "image_url":
    "data:<content_type>;base64,<data>"}` -- always `input_image`, since the
    Responses API has no `output_image` counterpart and only user-authored
    turns are expected to carry one in this design.
    """
    if isinstance(content, str):
        return {"type": "message", "role": role, "content": [{"type": part_type, "text": content}]}
    parts: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, TextPart):
            parts.append({"type": part_type, "text": part.text})
        elif isinstance(part, ImagePart):
            parts.append(
                {
                    "type": "input_image",
                    "image_url": (
                        f"data:{part.content_type};base64,{base64.b64encode(part.data).decode()}"
                    ),
                }
            )
    return {"type": "message", "role": role, "content": parts}


def to_responses_tools(tools: list[NeutralTool]) -> list[dict[str, Any]]:
    """Neutral tools -> flat Responses `FunctionTool` objects."""
    return [
        {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            # Required by FunctionTool. False because strict mode additionally
            # demands that every property appear in `required`, which oc8's
            # plugin-authored schemas do not guarantee.
            "strict": False,
            "parameters": tool.parameters,
        }
        for tool in tools
    ]


def build_responses_payload(req: CompletionRequest) -> dict[str, Any]:
    instructions, items = to_responses_input(req.messages)
    payload: dict[str, Any] = {"model": req.model, "input": items}
    if instructions:
        payload["instructions"] = instructions
    if req.tools:
        payload["tools"] = to_responses_tools(req.tools)
        payload["tool_choice"] = "auto"
    # The one `ModelParams` field forwarded (see point 4 in the module
    # docstring): it tunes the reasoning models this backend serves instead of
    # breaking them (spec 2026-11-11 §6).
    if req.params.reasoning is not None:
        payload["reasoning"] = {"effort": EFFORT_FOR_LEVEL[req.params.reasoning]}
    # Never leave a copy of a tenant's transcript on OpenAI's side for later
    # retrieval; Codex does the same (client.rs:889).
    payload["store"] = False
    return payload


def parse_responses_result(data: dict[str, Any], *, provider: str, model: str) -> CompletionResult:
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    text_parts.append(str(part.get("text") or ""))
        elif kind == "function_call":
            tool_calls.append(_tool_call_from_item(item))
        elif kind == "reasoning":
            reasoning_parts.extend(_reasoning_texts(item))
        # Every other item type carries nothing neutral.
    reasoning = "\n\n".join(reasoning_parts).strip()
    return CompletionResult(
        text="".join(text_parts),
        tool_calls=tool_calls,
        # `CompletionResult.usage` is not optional, so an unreported usage
        # becomes a zero here as it always has -- the non-streaming caller
        # holds the request and can price it itself. Only the STREAMED chunk
        # has to distinguish zero from unknown; see `_chunk_from_event`.
        usage=_usage_from(data.get("usage")) or Usage(),
        stop_reason=_stop_reason(data, saw_tool_call=bool(tool_calls)),
        provider=provider,
        model=model,
        reasoning=reasoning or None,
    )


def _reasoning_texts(item: dict[str, Any]) -> list[str]:
    """The readable text of a `reasoning` output item: its summary parts,
    then any full `reasoning_text` content the backend chose to return."""
    texts: list[str] = []
    for key, part_type in (("summary", "summary_text"), ("content", "reasoning_text")):
        for part in item.get(key) or []:
            if isinstance(part, dict) and part.get("type") == part_type and part.get("text"):
                texts.append(str(part["text"]))
    return texts


def _stop_reason(response: dict[str, Any], *, saw_tool_call: bool) -> str:
    if saw_tool_call:
        return "tool_use"
    # An `incomplete` response cut off by its output cap is "length", which the
    # runtimes' length retry tells apart from a finished answer.
    details = response.get("incomplete_details")
    if isinstance(details, dict) and details.get("reason") == "max_output_tokens":
        return "length"
    return "stop"


def _tool_call_from_item(item: dict[str, Any]) -> ToolCall:
    return ToolCall(
        # `call_id`, not `id`: a later function_call_output is paired to the
        # call by call_id, and `id` is rejected there.
        id=str(item.get("call_id") or ""),
        name=str(item.get("name") or ""),
        arguments=_loads_or_empty(item.get("arguments")),
    )


def _loads_or_empty(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _usage_from(raw: Any) -> Usage | None:
    """A `ResponseUsage` object -> `Usage`, or None when `raw` is not one.

    None means "the provider did not report usage", which is NOT the same as
    zero: `CompletionChunk.usage` is documented as staying None in that case
    precisely so `llm_gateway`'s `reported or Usage(..., estimate_tokens(...))`
    falls through to its estimate -- a `Usage(0, 0)` is a truthy dataclass and
    would defeat it, billing a whole streamed run at nothing. Same rule, same
    shape as `streaming._chunk_from_openai`, which only sets `chunk.usage`
    when a usage dict is genuinely present.
    """
    if not isinstance(raw, dict):
        return None
    return Usage(
        tokens_in=int(raw.get("input_tokens", 0) or 0),
        tokens_out=int(raw.get("output_tokens", 0) or 0),
        reasoning_tokens=reasoning_tokens_from(raw, "output_tokens_details"),
    )


async def post_responses(
    url: str, payload: dict[str, Any], headers: dict[str, str]
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json={**payload, "stream": False}, headers=headers)
        raise_for_status_with_body(resp)
        result: dict[str, Any] = resp.json()
        return result


async def stream_responses_sse(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[CompletionChunk]:
    """Stream a Responses API call as neutral chunks."""
    body = {**payload, "stream": True}
    owned = client is None
    c = client or httpx.AsyncClient(timeout=_TIMEOUT)
    saw_tool_call = False
    try:
        async with c.stream("POST", url, json=body, headers=headers) as resp:
            await araise_for_status_with_body(resp)
            async for line in resp.aiter_lines():
                line = line.strip()
                # `event:` lines repeat the type that is already in the payload;
                # comments (": keepalive") and blank separators carry nothing.
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[len("data:") :].strip())
                except (ValueError, TypeError):
                    logger.debug("skipping unparseable stream line: %r", line[:120])
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "response.failed":
                    raise RuntimeError(_failure_message(event))
                chunk = _chunk_from_event(event, saw_tool_call=saw_tool_call)
                if chunk is None:
                    continue
                if chunk.tool_calls:
                    saw_tool_call = True
                yield chunk
    finally:
        if owned:
            await c.aclose()


def _chunk_from_event(event: dict[str, Any], *, saw_tool_call: bool) -> CompletionChunk | None:
    """One SSE payload -> a chunk, or None if it carries nothing we forward."""
    kind = event.get("type")

    if kind == "response.output_text.delta":
        delta = event.get("delta")
        return CompletionChunk(text=str(delta)) if delta else None

    if kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
        delta = event.get("delta")
        return CompletionChunk(reasoning=str(delta)) if delta else None

    if kind == "response.reasoning_summary_part.done":
        # Separate one summary paragraph from the next, as the non-streamed
        # result does.
        return CompletionChunk(reasoning="\n\n")

    if kind == "response.output_item.done":
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "function_call":
            return None
        call = _tool_call_from_item(item)
        return CompletionChunk(
            tool_calls=[
                ToolCallDelta(
                    index=int(event.get("output_index", 0) or 0),
                    id=call.id,
                    name=call.name,
                    # Complete, not a fragment: the item carries the whole
                    # arguments string, so there is nothing to continue.
                    arguments_fragment=str(item.get("arguments") or ""),
                )
            ]
        )

    if kind in ("response.completed", "response.incomplete"):
        response = event.get("response")
        if not isinstance(response, dict):
            response = {}
        # None, never Usage(0, 0), when this event carries no parseable usage:
        # a zero here is indistinguishable from a real zero-token response and
        # silently bills the whole streamed run at nothing (see `_usage_from`).
        usage = _usage_from(response.get("usage"))
        return CompletionChunk(
            usage=usage, stop_reason=_stop_reason(response, saw_tool_call=saw_tool_call)
        )

    return None


def _failure_message(event: dict[str, Any]) -> str:
    response = event.get("response")
    error = response.get("error") if isinstance(response, dict) else None
    if isinstance(error, dict):
        return f"response.failed: {error.get('code')}: {error.get('message')}"
    return "response.failed"


class ChatGptSubscriptionAdapter:
    provider = "openai_chatgpt"

    def __init__(self, base_url: str, api_key: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._access_token, self._account_id = self._parse_api_key(api_key)

    @property
    def base_url(self) -> str:
        return self._base_url

    @staticmethod
    def _parse_api_key(api_key: str) -> tuple[str, str | None]:
        """`api_key` is a composite JSON string `{"access_token", "account_id"}`.

        The shared `ProviderEntry.factory` signature has exactly one string
        slot but this adapter needs two values, so Task 8's `resolve_model_key`
        packs both into it. Falls back to treating the whole value as a bare
        access token if it is not that shape, rather than crashing -- a missing
        account id degrades the request (no `ChatGPT-Account-ID` header), it
        does not break the adapter.
        """
        try:
            parsed = json.loads(api_key)
        except (ValueError, TypeError):
            return api_key, None
        if not isinstance(parsed, dict) or "access_token" not in parsed:
            return api_key, None
        # A present-but-null access_token stringifies to the literal "None",
        # which would ship as `Authorization: Bearer None`. Empty instead, so
        # `_headers` omits the header entirely -- same "degrade, never send
        # garbage" rule the account_id below already follows.
        return (
            str(parsed["access_token"]) if parsed.get("access_token") else "",
            str(parsed["account_id"]) if parsed.get("account_id") else None,
        )

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json", "originator": _ORIGINATOR}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        if self._account_id:
            headers["ChatGPT-Account-ID"] = self._account_id
        return headers

    @property
    def _url(self) -> str:
        return f"{self._base_url}/responses"

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        data = await post_responses(self._url, build_responses_payload(req), self._headers())
        return parse_responses_result(data, provider=self.provider, model=req.model)

    async def stream(self, req: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        async for chunk in stream_responses_sse(
            self._url, build_responses_payload(req), self._headers()
        ):
            yield chunk
