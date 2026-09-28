from __future__ import annotations

from oc8.capas.manifest import SetupMcpSpec


def test_setup_mcp_spec_command_is_optional() -> None:
    spec = SetupMcpSpec(name="remote_conn")
    assert spec.command == ""
