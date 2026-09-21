"""Explicit, context-local runtime composition for worker processes."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol

from oc8.runtime.supervision_hook import (
    NOOP_SUPERVISION_QUERY_PORT,
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

    def activate(self) -> AbstractContextManager[None]:
        return use_supervision_runtime(self._supervision_hook, self._query_port)


def _default_runtime_composition() -> RuntimeComposition:
    from oc8.supervision.runtime import SUPERVISION_QUERY_PORT, SUPERVISION_RUN_HOOK

    return RuntimeComposition(SUPERVISION_RUN_HOOK, SUPERVISION_QUERY_PORT)


class _DefaultRuntimeComposition:
    """Lazily bind the supervision hook so importing this module stays cheap."""

    def activate(self) -> AbstractContextManager[None]:
        return _default_runtime_composition().activate()


COMMUNITY_RUNTIME_COMPOSITION: EditionRuntimeComposition = _DefaultRuntimeComposition()
