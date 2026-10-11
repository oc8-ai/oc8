"""Explicit, context-local runtime composition for worker processes."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager
from typing import Protocol

from oc8.runtime.supervision_hook import (
    NOOP_SUPERVISION_QUERY_PORT,
    NOOP_SUPERVISION_RUN_HOOK,
    SupervisionQueryPort,
    SupervisionRunHook,
    use_supervision_runtime,
)


class EditionRuntimeComposition(Protocol):
    """Scopes edition runtime contributions to one worker job."""

    def activate(self) -> AbstractContextManager[None]: ...


class RuntimeComposition:
    """A small runtime composition containing the currently needed hook only."""

    def __init__(
        self,
        supervision_hook: SupervisionRunHook,
        query_port: SupervisionQueryPort = NOOP_SUPERVISION_QUERY_PORT,
    ) -> None:
        self._supervision_hook = supervision_hook
        self._query_port = query_port

    def activate(self) -> Iterator[None]:
        return use_supervision_runtime(self._supervision_hook, self._query_port)


COMMUNITY_RUNTIME_COMPOSITION = RuntimeComposition(NOOP_SUPERVISION_RUN_HOOK)
