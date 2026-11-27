"""What this process sent to a model: every completion request (with its
provider and the text of its messages) and every embedding call.

Installed once by the runner (`install`), it wraps the model router where
the engine, the checkpoint writer, retrieval, memory and titles get it, and
records before delegating -- the request as it went out, not the transcript
that came back. A scenario scores on it with `sent()`; the runner resets it
per attempt (`reset`) and reports the counts as metrics.

Only turns answered in THIS process are seen: run the runner with
`--serve-turns` (and the stack's worker stopped) for a scenario that scores
on requests.
"""

from __future__ import annotations

import importlib
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

#: The provider a request may carry restricted content to (`ModelRouter`'s
#: own guard): everything else is a non-local model.
LOCAL_PROVIDERS = frozenset({"ollama"})

#: Modules that fetch the router for a chat turn.
_USERS = (
    "oc8.agent.engine",
    "oc8.chat.checkpoint",
    "oc8.chat.titles",
    "oc8.knowledge.retrieval",
    "oc8.memory.router",
)


@dataclass(frozen=True)
class SentRequest:
    provider: str
    agent_id: uuid.UUID | None
    text: str

    @property
    def local(self) -> bool:
        return self.provider.lower() in LOCAL_PROVIDERS


_SENT: list[SentRequest] = []
_EMBEDS: list[int] = []


def _text(req: Any) -> str:
    parts = []
    for message in getattr(req, "messages", []) or []:
        content = getattr(message, "content", "")
        parts.append(content if isinstance(content, str) else repr(content))
    return "\n".join(parts)


class _Recording:
    """The router, recording each call before it is made."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _note(self, req: Any) -> None:
        _SENT.append(
            SentRequest(
                str(getattr(req, "provider", "")), getattr(req, "agent_id", None), _text(req)
            )
        )

    async def complete(self, req: Any) -> Any:
        self._note(req)
        return await self._inner.complete(req)

    async def stream(self, req: Any) -> AsyncIterator[Any]:
        self._note(req)
        async for chunk in self._inner.stream(req):
            yield chunk

    async def embed(self, text: Any, *args: Any, **kwargs: Any) -> Any:
        _EMBEDS.append(len(text) if isinstance(text, list) else 1)
        return await self._inner.embed(text, *args, **kwargs)

    async def embed_many(self, texts: Any, *args: Any, **kwargs: Any) -> Any:
        _EMBEDS.append(len(texts))
        return await self._inner.embed_many(texts, *args, **kwargs)


def install() -> None:
    """Wrap `get_model_router` in every module that fetches it for a turn.
    Idempotent; wraps whatever each module holds now (a test's fake too)."""
    for name in _USERS:
        module = sys.modules.get(name) or importlib.import_module(name)
        current = getattr(module, "get_model_router", None)
        if current is None or getattr(current, "_records", False):
            continue

        def _wrapped(_current: Any = current) -> Any:
            return _Recording(_current())

        _wrapped._records = True  # type: ignore[attr-defined]
        module.get_model_router = _wrapped  # type: ignore[attr-defined]


def reset() -> None:
    _SENT.clear()
    _EMBEDS.clear()


def sent(agent_id: uuid.UUID | None = None) -> list[SentRequest]:
    """The completion requests since `reset`, of one agent when given."""
    return [r for r in _SENT if agent_id is None or r.agent_id == agent_id]


def counts() -> dict[str, float]:
    """Metrics of the attempt: model calls and texts embedded."""
    return {"model_calls": float(len(_SENT)), "embedded_texts": float(sum(_EMBEDS))}
