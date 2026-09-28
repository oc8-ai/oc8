"""Advertised tool names route to the connection that owns them."""

from types import SimpleNamespace

from oc8.api.v1.internal_agent import _routed_connection


def test_a_jira_call_does_not_land_on_odoo() -> None:
    odoo = SimpleNamespace(name="odoo")
    jira = SimpleNamespace(name="jira")
    ctx = {"tool_routes": {"jira_create_issue": ["jira", "jira_create_issue"]}}
    routed = _routed_connection([odoo, jira], ctx, "jira_create_issue")
    assert routed is not None
    conn, tool = routed
    assert conn is jira
    assert tool == "jira_create_issue"


def test_without_a_route_table_the_caller_keeps_its_single_connection() -> None:
    assert _routed_connection([SimpleNamespace(name="odoo")], {}, "search_records") is None
