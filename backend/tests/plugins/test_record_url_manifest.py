"""`[connections.record_url]` -- how a tool pack declares where one of its
records lives (design 2026-09-27-ai-workplace-collaboration-design.md §6).

Data only. Nothing in core interprets the vendor's URL shape; core substitutes
three named placeholders and refuses anything else.
"""

from __future__ import annotations

import pytest

from oc8.capas.manifest import ManifestError, RecordUrlTemplate, ToolPackConnection, parse_manifest


def _conn(**record_url: object) -> ToolPackConnection:
    return ToolPackConnection.model_validate(
        {
            "key": "primary",
            "name": "odoo",
            "server_url": "",
            "transport": "stdio",
            "record_url": record_url,
        }
    )


def test_a_connection_without_a_declaration_has_none() -> None:
    conn = ToolPackConnection.model_validate(
        {"key": "primary", "name": "odoo", "server_url": "", "transport": "stdio"}
    )
    assert conn.record_url is None


def test_a_full_declaration_parses() -> None:
    conn = _conn(
        template="{base_url}/odoo/{model}/{id}",
        base_url_path=["env", "ODOO_URL"],
        models={"crm.lead": "crm.lead", "sale.order": "sale.order"},
    )
    assert conn.record_url == RecordUrlTemplate(
        template="{base_url}/odoo/{model}/{id}",
        base_url_path=["env", "ODOO_URL"],
        models={"crm.lead": "crm.lead", "sale.order": "sale.order"},
    )


def test_an_unknown_placeholder_is_refused() -> None:
    """Core substitutes exactly three names and declared placeholders. A template
    asking for an undeclared placeholder would raise KeyError deep inside a tool
    call -- caught here instead."""
    with pytest.raises(ValueError, match="undeclared placeholder"):
        _conn(
            template="{base_url}/odoo/{model}/{id}?company={company_id}",
            base_url_path=["env", "ODOO_URL"],
            models={"crm.lead": "crm.lead"},
        )


def test_a_template_with_no_base_url_is_refused() -> None:
    with pytest.raises(ValueError, match="must start with"):
        _conn(template="/odoo/{model}/{id}", base_url_path=["env", "ODOO_URL"], models={"a": "a"})


def test_a_base_url_placeholder_with_no_path_to_read_it_from_is_refused() -> None:
    with pytest.raises(ValueError, match="base_url_path"):
        _conn(template="{base_url}/odoo/{model}/{id}", models={"a": "a"})


def test_declaring_no_models_is_refused() -> None:
    """An empty `models` map can never produce a link, so it is a typo rather
    than a posture -- a pack that wants no links simply omits the table."""
    with pytest.raises(ValueError, match="no models"):
        _conn(template="{base_url}/x/{model}/{id}", base_url_path=["env", "URL"], models={})


def test_an_unknown_key_inside_the_table_is_refused() -> None:
    with pytest.raises(ValueError):
        _conn(
            template="{base_url}/x/{model}/{id}",
            base_url_path=["env", "URL"],
            models={"a": "a"},
            fallback="/x",
        )


def test_it_round_trips_through_parse_manifest() -> None:
    manifest = parse_manifest(
        {
            "name": "demo_pack",
            "version": "1.0.0",
            "type": "tool_pack",
            "summary": "s",
            "tool_pack": {
                "connections": [
                    {
                        "key": "primary",
                        "name": "demo",
                        "server_url": "",
                        "record_url": {
                            "template": "{base_url}/r/{model}/{id}",
                            "base_url_path": ["env", "DEMO_URL"],
                            "models": {"thing": "things"},
                        },
                    }
                ]
            },
        }
    )
    assert manifest.tool_pack is not None
    spec = manifest.tool_pack.connections[0].record_url
    assert spec is not None
    assert spec.models == {"thing": "things"}


def test_a_broken_declaration_surfaces_as_a_manifest_error() -> None:
    with pytest.raises(ManifestError):
        parse_manifest(
            {
                "name": "demo_pack",
                "version": "1.0.0",
                "type": "tool_pack",
                "summary": "s",
                "tool_pack": {
                    "connections": [
                        {
                            "key": "primary",
                            "name": "demo",
                            "server_url": "",
                            "record_url": {"template": "{nope}", "models": {"a": "a"}},
                        }
                    ]
                },
            }
        )


def test_extra_placeholders_need_a_declared_source() -> None:
    with pytest.raises(ValueError, match="owner"):
        RecordUrlTemplate(
            template="https://github.com/{owner}/{repo}/issues/{id}",
            models={"issue": "issues"},
            placeholders={"repo": ["arg:repo"]},
        )


def test_declared_placeholders_validate() -> None:
    spec = RecordUrlTemplate(
        template="https://github.com/{owner}/{repo}/issues/{id}",
        models={"issue": "issues", "pull": "pulls"},
        templates={"pull": "https://github.com/{owner}/{repo}/pull/{id}"},
        placeholders={"owner": ["arg:owner"], "repo": ["arg:repo", "item:repository.name"]},
        native_url_fields=["html_url"],
        native_url_hosts=["github.com"],
    )
    assert spec.templates_for("pull").endswith("/pull/{id}")
    assert spec.templates_for("issue").endswith("/issues/{id}")


def test_a_placeholder_source_must_name_a_known_kind() -> None:
    with pytest.raises(ValueError, match="arg:, item: or config:"):
        RecordUrlTemplate(
            template="https://x.example/{thing}/{id}",
            models={"a": "a"},
            placeholders={"thing": ["header:x"]},
        )


def test_native_fields_alone_are_enough() -> None:
    spec = RecordUrlTemplate(
        native_url_fields=["webUrl", "webLink"], native_url_hosts=["*.sharepoint.com"]
    )
    assert spec.template == ""


def test_native_fields_need_a_host_bound() -> None:
    with pytest.raises(ValueError, match="native_url_hosts or a base_url_path"):
        RecordUrlTemplate(native_url_fields=["webUrl"])


def test_nothing_declared_is_rejected() -> None:
    with pytest.raises(ValueError, match="can never produce a link"):
        RecordUrlTemplate()


def test_base_url_path_only_required_when_template_uses_base_url() -> None:
    RecordUrlTemplate(template="https://mail.google.com/mail/#all/{id}", models={"message": "m"})
    with pytest.raises(ValueError, match="base_url_path"):
        RecordUrlTemplate(template="{base_url}/browse/{id}", models={"issue": "issue"})


def test_a_template_override_must_name_a_listed_model() -> None:
    with pytest.raises(ValueError, match="templates"):
        RecordUrlTemplate(
            template="https://x.example/{model}/{id}",
            models={"issue": "issues"},
            templates={"pull": "https://x.example/pull/{id}"},
        )


def test_related_fields_must_name_a_listed_model() -> None:
    with pytest.raises(ValueError, match="related_fields"):
        RecordUrlTemplate(
            template="{base_url}/odoo/{model}/{id}",
            base_url_path=["env", "ODOO_URL"],
            models={"sale.order": "sale.order"},
            related_fields={"values.partner_id": "res.partner"},
        )
