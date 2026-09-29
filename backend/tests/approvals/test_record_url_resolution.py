"""Resolving a tool pack's `record_url` template (§6).

Every refusal here is a `None`, never an exception: this runs inside the
approval-raising path of a live tool call, and a cosmetic link must never be
able to stop a human being asked a question.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.approvals.record_url import record_url_for_connection, resolve_record_url
from oc8.capas.manifest import RecordUrlTemplate, ToolPackConnection

ODOO = RecordUrlTemplate(
    template="{base_url}/odoo/{model}/{id}",
    base_url_path=["env", "ODOO_URL"],
    models={"crm.lead": "crm.lead", "sale.order": "sale.order"},
)
CONFIG: dict[str, Any] = {"env": {"ODOO_URL": "https://odoo.example.com"}}


def test_a_declared_model_resolves() -> None:
    assert (
        resolve_record_url(ODOO, config=CONFIG, entity="sale.order", ref="42")
        == "https://odoo.example.com/odoo/sale.order/42"
    )


def test_no_spec_at_all_is_none() -> None:
    assert resolve_record_url(None, config=CONFIG, entity="sale.order", ref="42") is None


def test_an_undeclared_entity_is_none() -> None:
    """Only listed entities get a link -- same rule as `focus_spec.labels`."""
    assert resolve_record_url(ODOO, config=CONFIG, entity="helpdesk.ticket", ref="7") is None


def test_a_missing_base_url_is_none() -> None:
    assert resolve_record_url(ODOO, config={}, entity="sale.order", ref="42") is None


def test_a_blank_base_url_is_none() -> None:
    assert (
        resolve_record_url(ODOO, config={"env": {"ODOO_URL": "   "}}, entity="sale.order", ref="42")
        is None
    )


def test_a_trailing_slash_on_the_base_url_does_not_double_up() -> None:
    assert (
        resolve_record_url(
            ODOO, config={"env": {"ODOO_URL": "https://odoo.example.com/"}},
            entity="sale.order", ref="42",
        )
        == "https://odoo.example.com/odoo/sale.order/42"
    )


def test_an_empty_entity_or_ref_is_none() -> None:
    assert resolve_record_url(ODOO, config=CONFIG, entity="", ref="42") is None
    assert resolve_record_url(ODOO, config=CONFIG, entity="sale.order", ref="") is None


def test_a_non_http_base_url_is_refused() -> None:
    """A tool result is attacker-influenced input in the general case, and this
    string ends up in an `href`. Anything that is not http(s) is dropped."""
    assert (
        resolve_record_url(
            ODOO,
            config={"env": {"ODOO_URL": "javascript:alert(1)"}},
            entity="sale.order",
            ref="42",
        )
        is None
    )


def test_a_hostile_record_ref_is_percent_encoded() -> None:
    url = resolve_record_url(ODOO, config=CONFIG, entity="sale.order", ref="4 2/../../admin")
    assert url == "https://odoo.example.com/odoo/sale.order/4%202%2F..%2F..%2Fadmin"


def test_an_absurdly_long_result_is_dropped() -> None:
    assert resolve_record_url(ODOO, config=CONFIG, entity="sale.order", ref="9" * 2100) is None


def test_a_connection_with_no_plugin_stamp_is_none() -> None:
    conn = m.McpConnection(
        tenant_id=uuid.uuid4(), name="odoo", transport="stdio", server_url="", config={}
    )
    assert record_url_for_connection(conn, entity="sale.order", ref="42") is None


def test_no_connection_at_all_is_none() -> None:
    assert record_url_for_connection(None, entity="sale.order", ref="42") is None


def test_a_connection_resolves_through_its_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live path: the row carries `_plugin_name`/`_connection_key`, the
    manifest carries the template, the row's own config carries the host."""
    conn = m.McpConnection(
        tenant_id=uuid.uuid4(),
        name="odoo",
        transport="stdio",
        server_url="",
        config={
            "_plugin_name": "odoo_mcp",
            "_connection_key": "primary",
            "env": {"ODOO_URL": "https://odoo.example.com"},
        },
    )
    manifest_conn = ToolPackConnection(
        key="primary", name="odoo", server_url="", record_url=ODOO
    )
    monkeypatch.setattr(
        "oc8.approvals.record_url.resolve_tool_pack_connection",
        lambda plugin, key: manifest_conn if (plugin, key) == ("odoo_mcp", "primary") else None,
    )
    assert (
        record_url_for_connection(conn, entity="crm.lead", ref="7")
        == "https://odoo.example.com/odoo/crm.lead/7"
    )


def test_a_plugin_that_is_no_longer_on_disk_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = m.McpConnection(
        tenant_id=uuid.uuid4(),
        name="odoo",
        transport="stdio",
        server_url="",
        config={"_plugin_name": "odoo_mcp", "_connection_key": "primary"},
    )
    monkeypatch.setattr(
        "oc8.approvals.record_url.resolve_tool_pack_connection", lambda plugin, key: None
    )
    assert record_url_for_connection(conn, entity="crm.lead", ref="7") is None
