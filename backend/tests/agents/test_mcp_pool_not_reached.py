"""The pool says whether a failure happened before the tool was reached: a
usage limit gives a count back only then (a session that never opened sent
nothing), never for a call that reached the tool and then failed."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8.agent import mcp_pool
from oc8.agent.mcp_pool import never_reached_the_tool

pytestmark = pytest.mark.asyncio


class _NeverStarts:
    def __init__(self, *a: Any, **kw: Any) -> None:
        pass

    async def __aenter__(self) -> _NeverStarts:
        raise RuntimeError("the bridge did not start")

    async def __aexit__(self, *a: Any) -> None:
        return None


class _FailsOnCall(_NeverStarts):
    async def __aenter__(self) -> _FailsOnCall:
        return self

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        raise TimeoutError("no answer")


async def _call(monkeypatch: pytest.MonkeyPatch, session: type, reusable: bool) -> BaseException:
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", session)
    with pytest.raises(Exception) as caught:
        await mcp_pool.call(
            uuid.uuid4(),
            command="x",
            args=[],
            env={},
            tool="t",
            arguments={},
            reusable=reusable,
        )
    await mcp_pool.close_all()
    return caught.value


@pytest.mark.parametrize("reusable", [True, False])
async def test_a_session_that_never_opened_never_reached_the_tool(
    monkeypatch: pytest.MonkeyPatch, reusable: bool
) -> None:
    exc = await _call(monkeypatch, _NeverStarts, reusable)
    assert never_reached_the_tool(exc)
    assert "did not start" in str(exc)


@pytest.mark.parametrize("reusable", [True, False])
async def test_a_call_that_failed_after_opening_reached_it(
    monkeypatch: pytest.MonkeyPatch, reusable: bool
) -> None:
    exc = await _call(monkeypatch, _FailsOnCall, reusable)
    assert isinstance(exc, TimeoutError)
    assert not never_reached_the_tool(exc)
