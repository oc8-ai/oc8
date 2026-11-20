from __future__ import annotations

from oc8.capas.discovery import find_plugin
from oc8.capas.manifest import parse_manifest
from oc8.connections.integrations import integration_from_manifest


def _integration(plugin: str):  # type: ignore[no-untyped-def]
    found = find_plugin(plugin)
    assert found is not None and found.manifest is not None, plugin
    integration = integration_from_manifest(parse_manifest(found.manifest))
    assert integration is not None, plugin
    return integration


def test_every_connectable_plugin_is_recognised_with_a_readable_name() -> None:
    ms, google, odoo = (
        _integration("microsoft365"),
        _integration("google_workspace"),
        _integration("odoo_mcp"),
    )
    assert (ms.label, ms.kind) == ("Microsoft 365", "oauth")
    assert (google.label, google.provider) == ("Google Workspace", "google")
    assert (odoo.label, odoo.tool_key) == ("Odoo", "odoo")


def test_hubspot_and_jira_logins_map_onto_their_bridges() -> None:
    assert _integration("hubspot_mcp").credential_env_fields == {
        "PRIVATE_APP_ACCESS_TOKEN": "private_app_access_token",
        "HUBSPOT_PORTAL_ID": "portal_id",
        "HUBSPOT_UI_URL": "ui_url",
    }
    assert _integration("jira_mcp").credential_env_fields == {
        "JIRA_URL": "url",
        "JIRA_USERNAME": "username",
        "JIRA_API_TOKEN": "api_token",
    }


def test_moodle_token_maps_onto_its_bridge() -> None:
    moodle = _integration("moodle")
    assert (moodle.label, moodle.kind) == ("Moodle", "credential")
    assert moodle.credential_env_fields == {"MOODLE_URL": "url", "MOODLE_TOKEN": "token"}
