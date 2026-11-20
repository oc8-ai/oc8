"""A fake Moodle web service for the bridge's tool tests.

Imports nothing from this capa on purpose: each test module imports the
bridge itself (with the sys.modules eviction dance) and hands its OWN
`client` module object to `fake_moodle`, so the patch lands on exactly the
module its tools call through.
"""

from __future__ import annotations

from collections.abc import Callable
from types import ModuleType
from typing import Any

import pytest


class FakeMoodle:
    def __init__(self) -> None:
        self.answers: dict[str, Any] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def on(self, function: str, answer: Any) -> None:
        self.answers[function] = answer

    async def call(self, function: str, params: Any = None) -> Any:
        recorded = dict(params or {})
        self.calls.append((function, recorded))
        if function not in self.answers:
            raise AssertionError(f"unexpected Moodle call {function}")
        answer = self.answers[function]
        return answer(recorded) if callable(answer) else answer

    def params(self, function: str) -> dict[str, Any]:
        return next(p for f, p in self.calls if f == function)


@pytest.fixture
def fake_moodle(monkeypatch: pytest.MonkeyPatch) -> Callable[[ModuleType], FakeMoodle]:
    def install(client_module: ModuleType) -> FakeMoodle:
        fake = FakeMoodle()
        monkeypatch.setattr(client_module, "call", fake.call)
        return fake

    return install
