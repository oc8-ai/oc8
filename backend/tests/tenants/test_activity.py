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


async def test_a_raising_gate_logs_its_error_once_a_minute_per_organisation(
    caplog, monkeypatch
):
    import logging

    from oc8.tenants import activity

    now = {"t": 1000.0}
    monkeypatch.setattr(activity.time, "monotonic", lambda: now["t"])

    async def gate(tenant_id: uuid.UUID) -> bool:
        raise RuntimeError("unreachable")

    set_tenant_activity_gate(gate)
    first, second = uuid.uuid4(), uuid.uuid4()

    def errors() -> int:
        return sum(1 for r in caplog.records if r.levelno >= logging.ERROR)

    with caplog.at_level(logging.DEBUG, logger="oc8.tenants.activity"):
        assert await tenant_may_run(first) is True
        assert await tenant_may_run(first) is True
        now["t"] += 59
        assert await tenant_may_run(first) is True
        assert errors() == 1
        assert any(r.levelno == logging.DEBUG for r in caplog.records)

        assert await tenant_may_run(second) is True
        assert errors() == 2

        now["t"] += 1
        assert await tenant_may_run(first) is True
        assert errors() == 3


def test_one_vocabulary_for_an_organisation_that_may_not_run():
    from oc8.tenants import activity

    assert activity.TENANT_INACTIVE_KIND == "tenant_inactive"
    assert activity.TENANT_INACTIVE_REASON == "organisation is not active"
    assert activity.TENANT_INACTIVE_OUTPUT == "Cancelled: organisation is not active"
