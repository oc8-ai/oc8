"""Provider-neutral request/response types."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class TextPart:
    text: str


@dataclass
class ImagePart:
    data: bytes
    content_type: str  # e.g. "image/png"


ContentPart = TextPart | ImagePart


@dataclass
class NeutralMessage:
    role: Role
    content: str | list[ContentPart] = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None  # set on role="tool" results
    name: str | None = None  # tool name on role="tool"


@dataclass
class NeutralTool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema (draft 2020-12)
    annotations: dict[str, Any] | None = None


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass
class CompletionResult:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage
    stop_reason: str
    provider: str
    model: str


@dataclass
class ToolCallDelta:
    """A fragment of a streamed tool call.

    Arguments arrive split across chunks, so a fragment is NOT valid JSON on its
    own. ``index`` identifies which call it belongs to -- a model may stream
    several in parallel, and merging them by arrival order would corrupt both.
    ``id`` and ``name`` usually appear once, on the first fragment.
    """

    index: int
    id: str | None = None
    name: str | None = None
    arguments_fragment: str = ""


@dataclass
class CompletionChunk:
    """One incremental piece of a streamed completion.

    ``usage`` and ``stop_reason`` appear only on the final chunk(s). ``usage`` may
    stay None for the whole stream if the provider never reports it -- the caller
    must treat that as "unknown", never as zero, or a streamed run bills nothing.
    """

    text: str = ""
    tool_calls: list[ToolCallDelta] = field(default_factory=list)
    usage: Usage | None = None
    stop_reason: str | None = None
    #: Stamped by ModelRouter.stream() with whatever resolve() actually
    #: decided (not every adapter sets these itself) so a caller
    #: reconstructing a CompletionResult from a stream -- oc8.modelrouter.
    #: accumulate -- knows which provider/model actually answered even
    #: after a BYOK downgrade or a mid-chain fallback, without threading
    #: that state through separately.
    provider: str | None = None
    model: str | None = None


@dataclass
class ModelParams:
    temperature: float = 0.2
    max_tokens: int = 1024
    #: Forwarded to the provider verbatim, whatever it currently accepts
    #: ("low"/"medium"/"high", a number-as-string, ...) -- unlike temperature
    #: there is no oc8-side notion of a valid range, since the provider owns
    #: that set and changes it without oc8's involvement. None omits the
    #: field entirely rather than sending a guessed default.
    effort: str | None = None
    #: Free-form top-level request fields an operator wants forwarded verbatim
    #: (e.g. OpenRouter's `provider`/`top_p`) that oc8 has no named field for.
    #: Merged into the outbound payload strictly AFTER every named field, so a
    #: key that collides with one oc8 already sets (model/messages/tools/...)
    #: is dropped rather than clobbering it -- see each adapter's payload
    #: builder. None/empty omits the merge entirely.
    extra: dict[str, Any] | None = None


@dataclass
class CompletionRequest:
    provider: str
    model: str
    messages: list[NeutralMessage]
    tools: list[NeutralTool] = field(default_factory=list)
    params: ModelParams = field(default_factory=ModelParams)
    tenant_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    request_id: uuid.UUID | None = None
    contains_restricted: bool = False
    base_url: str | None = None
    api_key: str | None = None


class ModelAdapter(Protocol):
    provider: str

    async def complete(self, req: CompletionRequest) -> CompletionResult: ...
