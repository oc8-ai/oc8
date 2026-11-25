"""`stage_carried_over_turn` with both deliveries (spec §5.5): what a DONE run
never read -- steer and queued alike -- becomes ONE turn in acceptance order."""

from __future__ import annotations

import pytest

from oc8.runtime.run_inputs import CARRIED_OVER_STEP
from tests.conftest import AppSessionFactory
from tests.runtime.test_carried_over_inputs import _execute, _state, _world
from tests.runtime.test_run_inputs_delivery import _ending

pytestmark = pytest.mark.asyncio


async def test_unread_steer_and_next_turn_join_in_seq_order(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    world = await _world(app_session, messenger=False)

    await _execute(
        world,
        _ending(
            app_session,
            world,
            status="done",
            writes=(("Erstens.", "next_turn"), ("Zweitens.", "steer"), ("Drittens.", "next_turn")),
        ),
    )

    runs, messages, inputs = await _state(app_session, world)
    assert [r.state for r in runs] == ["done", "queued"]
    assert [i.consumed_step for i in inputs] == [CARRIED_OVER_STEP] * 3
    assert messages[-1].content == "Erstens.\n\nZweitens.\n\nDrittens."
