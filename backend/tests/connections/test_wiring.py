"""Every launch path resolves the run's access (source check, like the app-shell
wiring tests): a path that kept the old per-connection environment would hand
the department's or nobody's token to a person's call."""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "oc8"


def _read(rel: str) -> str:
    return (SRC / rel).read_text()


def test_gateway_and_isolated_runtime_resolve_per_run() -> None:
    for rel in ("api/mcp_gateway.py", "api/v1/internal_agent.py"):
        text = _read(rel)
        assert "run_mcp_env(" in text, rel
        assert "reusable=not has_oauth_ref(cfg)" not in text, rel
        assert "tenant_id=tenant_id, cfg=_cfg(conn)" not in text, rel


def test_the_in_process_engine_resolves_per_run() -> None:
    text = _read("agent/engine.py")
    assert "run_mcp_env(" in text
    assert "integration_connections(" in text


def test_reading_a_resource_resolves_per_run() -> None:
    # The read_resource control tool launches the same bridge outside the
    # three paths above: it must not fall back to the department's account.
    assert "run_mcp_env(" in _read("agent/control_tools.py")
