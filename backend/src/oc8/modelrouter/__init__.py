"""Model Router — the LLM-agnostic layer (tech-spec §9).

Neutral messages and tool schemas go in; provider adapters translate at call
time. Switching an agent's model is a config change, never a data migration.
"""

from oc8.modelrouter.fallback import (
    complete_with_fallback,
    is_retryable,
    stream_completion_with_fallback,
)
from oc8.modelrouter.router import (
    ClassificationViolation,
    EmbeddingMustStayLocal,
    EmbeddingUnavailable,
    ModelRouter,
    get_model_router,
    locality_for_provider,
)
from oc8.modelrouter.streaming import chunk_from_result, estimate_tokens, stream_with_fallback
from oc8.modelrouter.types import (
    CompletionChunk,
    CompletionRequest,
    CompletionResult,
    NeutralMessage,
    NeutralTool,
    ToolCall,
    ToolCallDelta,
    Usage,
)

__all__ = [
    "ClassificationViolation",
    "CompletionChunk",
    "CompletionRequest",
    "CompletionResult",
    "EmbeddingMustStayLocal",
    "EmbeddingUnavailable",
    "ModelRouter",
    "NeutralMessage",
    "NeutralTool",
    "ToolCall",
    "ToolCallDelta",
    "Usage",
    "chunk_from_result",
    "complete_with_fallback",
    "estimate_tokens",
    "get_model_router",
    "is_retryable",
    "locality_for_provider",
    "stream_completion_with_fallback",
    "stream_with_fallback",
]
