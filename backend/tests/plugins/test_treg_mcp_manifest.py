"""treg_mcp is deliberately unlike every other tool-pack plugin in this repo:
its tool surface is not known at manifest-authoring time, so it ships no
static `scopes` classification and no `guardrails/` presets (design:
2026-09-19-custom-mcp-capa-design.md's Non-goal for custom capas, applied
here to a first-party one with the same "the catalog is not fixed" problem).
This reads the REAL plugin.toml/tool_pack.toml/setup/*.toml/credential_types/
files and asserts that absence is deliberate, not an oversight -- and that
the one thing this connection DOES rely on (a generic auth-header lookup
already proven by test_open_tool_session.py) resolves correctly for treg's
own config shape."""

from __future__ import annotations

from pathlib import Path

from oc8.agent.mcp_client import resolve_auth_header
from oc8.capas.discovery import DiscoveredPlugin, find_plugin
from oc8.capas.manifest import ToolPackConnection, parse_manifest

# tests/plugins/<this> -> tests -> backend -> repo root, where capas/ lives.
_PLUGINS_DIR = Path(__file__).resolve().parents[3] / "capas"


def _discovered() -> DiscoveredPlugin:
    found = find_plugin("treg_mcp", [str(_PLUGINS_DIR)])
    assert found is not None, "treg_mcp plugin not found"
    assert found.valid, found.error
    return found


def _connection() -> ToolPackConnection:
    found = _discovered()
    assert found.manifest is not None
    manifest = parse_manifest(found.manifest)
    assert manifest.tool_pack is not None
    return manifest.tool_pack.connections[0]


def test_plugin_is_first_party_tool_pack() -> None:
    found = _discovered()
    assert found.type == "tool_pack"
    assert found.trust == "first_party"


def test_connection_speaks_http_to_the_curated_v2_endpoint() -> None:
    conn = _connection()
    assert conn.key == "primary"
    assert conn.transport == "http"
    assert conn.server_url == "https://treg.to/mcp/v2/"


def test_connection_authenticates_with_one_header_and_no_static_scopes() -> None:
    conn = _connection()
    assert conn.config == {"auth_header_name": "X-Treg-Token"}
    assert conn.credential_type == "treg_token"
    # Deliberate: the real tool list only exists on the tenant's own treg
    # account and can change without an oc8 release. See tool_pack.toml.
    assert conn.scopes == []


def test_no_guardrail_presets_ship_because_there_is_nothing_to_gate_against() -> None:
    # A preset's read/modify grant is checked against the connection's own
    # `scopes` classification (authz/pdp.py) -- an empty `scopes` gives a
    # preset nothing to gate, so shipping one here would be untested,
    # unenforceable decoration.
    assert _connection().guardrail_presets == []
    assert _discovered().guardrail_library is None


def test_setup_form_asks_for_a_department_and_a_treg_token_credential() -> None:
    found = _discovered()
    assert found.manifest is not None
    setup = parse_manifest(found.manifest).setup
    assert setup is not None
    fields_by_key = {f.key: f for f in setup.fields}
    assert set(fields_by_key) == {"department", "token"}
    assert fields_by_key["department"].kind == "department"
    assert fields_by_key["department"].required is False
    assert fields_by_key["token"].kind == "credential"
    assert fields_by_key["token"].credential_type == "treg_token"
    assert fields_by_key["token"].required is True


def test_setup_wires_the_token_field_into_the_x_treg_token_header() -> None:
    found = _discovered()
    assert found.manifest is not None
    setup = parse_manifest(found.manifest).setup
    assert setup is not None
    mcp = setup.mcp
    assert mcp is not None
    assert mcp.connection_key == "primary"
    assert mcp.name == "treg"
    # Non-stdio transport: nothing reads config["command"] for it
    # (agent/mcp_client.py's open_tool_session).
    assert mcp.command == ""
    assert mcp.secret_env_fields == {"X-Treg-Token": "token"}
    assert mcp.department_field == "department"


def test_treg_token_credential_type_is_a_single_password_field() -> None:
    found = _discovered()
    assert found.manifest is not None
    cred_types = {ct["name"]: ct for ct in found.manifest.get("credential_types", [])}
    assert set(cred_types) == {"treg_token"}
    fields = cred_types["treg_token"]["fields"]
    assert [f["key"] for f in fields] == ["token"]
    assert fields[0]["kind"] == "password"


def test_resolve_auth_header_turns_a_resolved_secret_into_the_real_treg_header() -> None:
    """The exact mechanism `configure_plugin` and the runtime executor rely on
    for this connection, proven end-to-end for treg's OWN config shape rather
    than assumed by analogy from the custom-mcp-capa e2e test's generic
    `Authorization` example."""
    conn = _connection()
    env = {"X-Treg-Token": "s3cr3t-treg-token"}
    assert resolve_auth_header(conn.config, env) == {"X-Treg-Token": "s3cr3t-treg-token"}
