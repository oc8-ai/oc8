"""The generic run loop's supervision seam is context-local and fail-safe."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from oc8.runtime.supervision_hook import (
    NOOP_SUPERVISION_QUERY_PORT,
    NOOP_SUPERVISION_RUN_HOOK,
    SupervisionRunHook,
    current_supervision_query_port,
    current_supervision_run_hook,
    use_supervision_run_hook,
)


def test_default_community_hook_is_noop() -> None:
    assert current_supervision_run_hook() is NOOP_SUPERVISION_RUN_HOOK


def test_hook_override_is_context_local_and_resets() -> None:
    original = current_supervision_run_hook()

    with use_supervision_run_hook(NOOP_SUPERVISION_RUN_HOOK):
        assert current_supervision_run_hook() is NOOP_SUPERVISION_RUN_HOOK

    assert current_supervision_run_hook() is original


@pytest.mark.asyncio
async def test_hook_overrides_are_isolated_between_concurrent_tasks() -> None:
    original = current_supervision_run_hook()

    async def observe(hook: SupervisionRunHook) -> SupervisionRunHook:
        with use_supervision_run_hook(hook):
            await asyncio.sleep(0)
            return current_supervision_run_hook()

    noop_seen, legacy_seen = await asyncio.gather(
        observe(NOOP_SUPERVISION_RUN_HOOK),
        observe(original),
    )

    assert noop_seen is NOOP_SUPERVISION_RUN_HOOK
    assert legacy_seen is original
    assert current_supervision_run_hook() is original


@pytest.mark.asyncio
async def test_noop_hook_does_not_require_a_database_session() -> None:
    hook = NOOP_SUPERVISION_RUN_HOOK
    tenant_id = uuid.uuid4()

    assert (
        await hook.create_anchor(
            None,  # type: ignore[arg-type]
            tenant_id=tenant_id,
            agent_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_text="test",
        )
        is None
    )
    assert (
        await hook.checkpoint(
            None,  # type: ignore[arg-type]
            tenant_id=tenant_id,
            agent_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            anchor=None,
            tool_trace_delta=[],
            tokens_since_checkpoint=0,
            force=False,
        )
        is None
    )


@pytest.mark.asyncio
async def test_default_query_port_is_fail_closed_without_a_database() -> None:
    assert current_supervision_query_port() is NOOP_SUPERVISION_QUERY_PORT
    assert (
        await current_supervision_query_port().has_supervision(
            None,  # type: ignore[arg-type]
            agent_id=uuid.uuid4(),
        )
        is False
    )


def test_app_runtime_composition_installs_the_supervision_hook() -> None:
    from oc8.edition.runtime import COMMUNITY_RUNTIME_COMPOSITION
    from oc8.supervision.runtime import SUPERVISION_QUERY_PORT, SUPERVISION_RUN_HOOK

    with COMMUNITY_RUNTIME_COMPOSITION.activate():
        assert current_supervision_run_hook() is SUPERVISION_RUN_HOOK
        assert current_supervision_query_port() is SUPERVISION_QUERY_PORT
