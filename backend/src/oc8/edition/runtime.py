"""Explicit, context-local runtime composition for worker processes."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from typing import Protocol

from oc8.chat.audience import ExtraReaders, use_extra_readers
from oc8.edition.tools import (
    EditionTool,
    TenantEntitlements,
    community_entitlements,
    index_edition_tools,
    use_edition_tools,
)
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
    """The edition seams one process runs with: the supervision hook and query
    port, the edition tools with the per-tenant entitlements that gate them, and the chat readers
    an edition admits (read-only sharing).

    The API and the worker must receive the same composition -- the edition's
    entry point builds it once and passes it to both -- or a tool offered in
    one would be unknown in the other.
    """

    def __init__(
        self,
        supervision_hook: SupervisionRunHook,
        query_port: SupervisionQueryPort = NOOP_SUPERVISION_QUERY_PORT,
        edition_tools: Sequence[EditionTool] = (),
        tenant_entitlements: TenantEntitlements = community_entitlements,
        extra_readers: ExtraReaders | None = None,
    ) -> None:
        # Validated here, not on first activation: a name clash stops startup.
        index_edition_tools(edition_tools)
        self._supervision_hook = supervision_hook
        self._query_port = query_port
        self._edition_tools = tuple(edition_tools)
        self._tenant_entitlements = tenant_entitlements
        self._extra_readers = extra_readers

    def with_tools(self, tools: Sequence[EditionTool]) -> RuntimeComposition:
        """This composition plus `tools` (e.g. collected from the edition
        extensions). Raises ValueError on a name clash."""
        return RuntimeComposition(
            self._supervision_hook,
            self._query_port,
            edition_tools=(*self._edition_tools, *tools),
            tenant_entitlements=self._tenant_entitlements,
            extra_readers=self._extra_readers,
        )

    @contextmanager
    def activate(self) -> Iterator[None]:
        with use_supervision_runtime(self._supervision_hook, self._query_port):
            with use_edition_tools(self._edition_tools, self._tenant_entitlements):
                with use_extra_readers(self._extra_readers):
                    yield


COMMUNITY_RUNTIME_COMPOSITION = RuntimeComposition(NOOP_SUPERVISION_RUN_HOOK)
