"""The address an operator clicks to open the record a tool call named."""

from oc8.agent.record_link import odoo_form_url, record_url_from_env


def test_odoo_form_url_names_the_record() -> None:
    url = odoo_form_url("https://odoo.example/", "helpdesk.ticket", "#43")
    assert url == "https://odoo.example/web#id=43&model=helpdesk.ticket&view_type=form"


def test_odoo_form_url_refuses_a_search_and_a_bare_host() -> None:
    assert odoo_form_url("https://odoo.example", "helpdesk.ticket", "open") is None
    assert odoo_form_url("odoo.example", "helpdesk.ticket", "43") is None
    assert odoo_form_url("", "helpdesk.ticket", "43") is None


def test_jira_browse_url_names_the_issue() -> None:
    env = {"JIRA_URL": "https://jira.example/"}
    assert (
        record_url_from_env("jira", env, ("issue", "SUP-14"))
        == "https://jira.example/browse/SUP-14"
    )
    assert record_url_from_env("jira", env, ("issue", "open")) is None
    assert record_url_from_env("microsoft365", env, ("message", "1")) is None
    assert record_url_from_env("odoo", {"ODOO_URL": "https://odoo.example"}, None) is None
