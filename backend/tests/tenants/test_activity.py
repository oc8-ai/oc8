import uuid

import pytest

from oc8.tenants.activity import (
    TenantInactive,
    get_tenant_activity_gate,
    set_tenant_activity_gate,
    tenant_may_run,
)


@pytest.fixture(autouse=True)
def _no_gate():
    set_tenant_activity_gate(None)
    yield
    set_tenant_activity_gate(None)


async def test_without_a_gate_every_organisation_may_run():
    assert get_tenant_activity_gate() is None
    assert await tenant_may_run(uuid.uuid4()) is True


async def test_the_gate_decides_per_organisation():
    stopped = uuid.uuid4()

    async def gate(tenant_id: uuid.UUID) -> bool:
        return tenant_id != stopped

    set_tenant_activity_gate(gate)
    assert await tenant_may_run(stopped) is False
    assert await tenant_may_run(uuid.uuid4()) is True


async def test_a_gate_that_raises_counts_as_may_run(caplog):
    async def gate(tenant_id: uuid.UUID) -> bool:
        raise RuntimeError("unreachable")

    set_tenant_activity_gate(gate)
    assert await tenant_may_run(uuid.uuid4()) is True
    assert "activity gate" in caplog.text


def test_tenant_inactive_names_the_organisation():
    tenant_id = uuid.uuid4()
    assert TenantInactive(tenant_id).tenant_id == tenant_id
