"""`effort` (feature request: "switch effort in Claude models") is forwarded
to Anthropic verbatim as `output_config.effort`, never validated against a
fixed set of values -- the provider owns what it accepts and that changes on
its own schedule.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from oc8.modelrouter.adapters.anthropic import AnthropicAdapter
from oc8.modelrouter.types import CompletionRequest, ModelParams, NeutralMessage

pytestmark = pytest.mark.asyncio

_OK_BODY = {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": {}}


def _req(effort: str | None) -> CompletionRequest:
    return CompletionRequest(
        provider="anthropic",
        model="claude-sonnet-5",
        messages=[NeutralMessage(role="user", content="hi")],
        params=ModelParams(temperature=0.3, max_tokens=64, effort=effort),
    )


async def test_a_configured_effort_is_sent_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_post(
        self: httpx.AsyncClient, url: str, json: Any = None, **kw: Any
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(200, json=_OK_BODY, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    adapter = AnthropicAdapter(api_key="sk-ant-test")

    await adapter.complete(_req("medium-high"))

    assert calls[0]["output_config"] == {"effort": "medium-high"}
    assert "effort" not in calls[0]


async def test_no_effort_configured_omits_the_field(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_post(
        self: httpx.AsyncClient, url: str, json: Any = None, **kw: Any
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(200, json=_OK_BODY, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    adapter = AnthropicAdapter(api_key="sk-ant-test")

    await adapter.complete(_req(None))

    assert "effort" not in calls[0]
    assert "output_config" not in calls[0]
