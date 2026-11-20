"""Shared test factories.

Plain async functions, not fixtures -- a caller supplies the `app_session`
factory (see `tests/conftest.py::AppSessionFactory`) the same way every other
test in this suite does, so a factory here cannot accidentally create rows
under a tenant different from the one a caller's own session is bound to.
"""

from __future__ import annotations

import uuid
from typing import Any

from oc8 import models as m
from oc8.agents.versioning import publish_version
from tests.conftest import AppSessionFactory


async def _make_agent(app_session: AppSessionFactory, **overrides: Any) -> m.Agent:
    """Create a fresh tenant + department + agent, publish it as v1 via
    `publish_version`, and return the (detached) agent.

    `overrides` are passed straight to `m.Agent(...)`, so a caller can set
    e.g. `mission="…"` or `is_team_lead=True` without this helper growing a
    parameter per column. `tenant_id` may be overridden too, to put the
    agent in a caller-chosen tenant (e.g. one it also seeded other rows
    into); by default each call gets its own fresh tenant so tests never
    collide the way a shared tenant would (see `ACME_TENANT_ID` note in
    tests/conftest.py).
    """
    tenant_id = overrides.pop("tenant_id", None) or uuid.uuid4()
    name = overrides.pop("name", "Test Agent")
    async with app_session(tenant_id) as db:
        dept = m.Department(tenant_id=tenant_id, name="Eng", frame={})
        db.add(dept)
        await db.flush()

        agent = m.Agent(
            tenant_id=tenant_id,
            department_id=dept.id,
            name=name,
            **overrides,
        )
        db.add(agent)
        await db.flush()

        await publish_version(db, agent)
    return agent
