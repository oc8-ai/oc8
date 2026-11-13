"""Record links (spec 2026-11-13-record-links-design.md §4.2). Every failure is
"no link", never an exception: this runs inside live tool calls."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from oc8.agent.tool_idempotency import REPLAY_NOTE
from oc8.capas.manifest import RecordUrlTemplate
from oc8.records.links import (
    MAX_LINKS_PER_RESULT,
    enrich,
    enrich_and_remember,
    link_context_from,
    link_for,
    related_links,
)
from tests.conftest import AppSessionFactory

ODOO = RecordUrlTemplate(
    template="{base_url}/odoo/{model}/{id}",
    base_url_path=["env", "ODOO_URL"],
    models={"sale.order": "sale.order", "res.partner": "res.partner"},
    related_fields={"values.partner_id": "res.partner"},
)
ODOO_FOCUS: dict[str, Any] = {
    "entity_field": "model",
    "id_fields": ["id", "record_id"],
    "id_field": "id",
    "search_tools": ["search_records"],
    "labels": {"sale.order": "Angebot", "res.partner": "Kontakt"},
}
GITHUB = RecordUrlTemplate(
    template="https://github.com/{owner}/{repo}/issues/{id}",
    models={"issue": "issues", "pull": "pulls"},
    templates={"pull": "https://github.com/{owner}/{repo}/pull/{id}"},
    placeholders={"owner": ["arg:owner"], "repo": ["arg:repo"]},
    native_url_fields=["html_url"],
    native_url_hosts=["github.com"],
)
GITHUB_FOCUS: dict[str, Any] = {
    "id_fields": ["issue_number", "number"],
    "tool_entities": {"issue_read": "issue", "list_issues": "issue", "pull_request_read": "pull"},
}


def _odoo(env: dict[str, str] | None = None, config: dict[str, Any] | None = None):  # type: ignore[no-untyped-def]
    cfg = (
        config
        if config is not None
        else {"env": {"ODOO_URL": "http://placeholder:1000"}, "focus_spec": ODOO_FOCUS}
    )
    return link_context_from(
        ODOO, config=cfg, env=env if env is not None else {"ODOO_URL": "https://odoo.example.com"}
    )


def test_resolved_env_wins_over_config_env() -> None:
    assert (
        link_for(_odoo(), entity="sale.order", ref="42")
        == "https://odoo.example.com/odoo/sale.order/42"
    )


def test_config_env_is_the_fallback() -> None:
    ctx = _odoo(env={})
    assert (
        link_for(ctx, entity="sale.order", ref="42") == "http://placeholder:1000/odoo/sale.order/42"
    )


def test_unlisted_entity_and_empty_ref_have_no_link() -> None:
    assert link_for(_odoo(), entity="helpdesk.ticket", ref="1") is None
    assert link_for(_odoo(), entity="sale.order", ref="") is None


def test_placeholders_come_from_arguments_and_are_encoded() -> None:
    ctx = link_context_from(GITHUB, config={"focus_spec": GITHUB_FOCUS})
    url = link_for(ctx, entity="pull", ref="7", args={"owner": "oc8-ai", "repo": "a/b"})
    assert url == "https://github.com/oc8-ai/a%2Fb/pull/7"


def test_a_missing_placeholder_means_no_link() -> None:
    ctx = link_context_from(GITHUB, config={"focus_spec": GITHUB_FOCUS})
    assert link_for(ctx, entity="issue", ref="7", args={"owner": "oc8-ai"}) is None


def test_a_native_link_is_preferred_and_must_be_http() -> None:
    ctx = link_context_from(GITHUB, config={"focus_spec": GITHUB_FOCUS})
    item = {"number": 7, "html_url": "https://github.com/oc8-ai/oc8/issues/7"}
    assert (
        link_for(ctx, entity="issue", ref="7", item=item)
        == "https://github.com/oc8-ai/oc8/issues/7"
    )
    assert link_for(ctx, entity="", ref="", item={"html_url": "javascript:alert(1)"}) is None


def test_enrich_marks_every_record_of_a_search() -> None:
    out = json.dumps([{"id": 1, "name": "S1"}, {"id": 2, "name": "S2"}])
    new, issued = enrich(_odoo(), "search_records", {"model": "sale.order"}, out)
    data = json.loads(new)
    assert (
        [d["oc8_url"] for d in data]
        == issued
        == [
            "https://odoo.example.com/odoo/sale.order/1",
            "https://odoo.example.com/odoo/sale.order/2",
        ]
    )


def test_enrich_walks_one_container_level_only() -> None:
    ctx = link_context_from(GITHUB, config={"focus_spec": GITHUB_FOCUS})
    out = json.dumps(
        {
            "items": [
                {
                    "number": 3,
                    "html_url": "https://github.com/o/r/issues/3",
                    "user": {"html_url": "https://github.com/someone"},
                }
            ]
        }
    )
    new, issued = enrich(ctx, "list_issues", {"owner": "o", "repo": "r"}, out)
    data = json.loads(new)
    assert data["items"][0]["oc8_url"] == "https://github.com/o/r/issues/3"
    assert "oc8_url" not in data["items"][0]["user"]
    assert issued == ["https://github.com/o/r/issues/3"]


def test_enrich_caps_at_fifty_links() -> None:
    out = json.dumps([{"id": i} for i in range(MAX_LINKS_PER_RESULT + 10)])
    new, issued = enrich(_odoo(), "search_records", {"model": "sale.order"}, out)
    data = json.loads(new)
    assert len(issued) == MAX_LINKS_PER_RESULT
    assert "oc8_url" in data[MAX_LINKS_PER_RESULT - 1]
    assert "oc8_url" not in data[MAX_LINKS_PER_RESULT]


def test_enrich_unset_base_leaves_output_untouched() -> None:
    ctx = _odoo(env={}, config={"focus_spec": ODOO_FOCUS})
    out = '[{"id": 1}]'
    assert enrich(ctx, "search_records", {"model": "sale.order"}, out) == (out, [])


def test_enrich_ignores_non_json_and_no_context() -> None:
    assert enrich(_odoo(), "search_records", {"model": "sale.order"}, "plain text") == (
        "plain text",
        [],
    )
    assert enrich(None, "x", {}, '[{"id": 1}]') == ('[{"id": 1}]', [])


def test_related_links_accept_odoo_many2one_shapes() -> None:
    ctx = _odoo()
    assert related_links(ctx, {"values": {"partner_id": 7}}) == [
        {"label": "Kontakt #7", "url": "https://odoo.example.com/odoo/res.partner/7"}
    ]
    # The model-supplied display name is never shown: it could spoof the record.
    assert related_links(ctx, {"values": {"partner_id": [7, "XYZ GmbH"]}}) == [
        {"label": "Kontakt #7", "url": "https://odoo.example.com/odoo/res.partner/7"}
    ]
    assert related_links(ctx, {"values": {"partner_id": False}}) == []


def test_enrich_survives_pathologically_nested_json() -> None:
    out = "[" * 100000 + "]" * 100000
    assert enrich(_odoo(), "search_records", {"model": "sale.order"}, out) == (out, [])


async def test_enrich_and_remember_registers_what_it_issued(
    app_session: AppSessionFactory,
) -> None:
    from oc8.records.registry import issued_links

    tenant = uuid.uuid4()
    url = "https://odoo.example.com/odoo/sale.order/42"
    async with app_session(tenant) as db:
        new, issued = await enrich_and_remember(
            db,
            tenant_id=tenant,
            scope="task:1",
            ctx=_odoo(),
            tool_name="search_records",
            arguments={"model": "sale.order"},
            output='[{"id": 42, "name": "S00042"}]',
        )
        assert issued == [url]
        assert json.loads(new)[0]["oc8_url"] == url
        assert await issued_links(db, tenant_id=tenant, scope="task:1", urls=[url]) == {url}
        assert await issued_links(db, tenant_id=tenant, scope="task:2", urls=[url]) == set()


async def test_a_replayed_result_is_enriched_like_the_first_one(
    app_session: AppSessionFactory,
) -> None:
    """A replay carries the idempotency note after the JSON. The model must see
    the same links on a resume as on the first run, note included."""
    tenant = uuid.uuid4()
    raw = '[{"id": 42}]'
    async with app_session(tenant) as db:
        first, _ = await enrich_and_remember(
            db,
            tenant_id=tenant,
            scope="task:1",
            ctx=_odoo(),
            tool_name="search_records",
            arguments={"model": "sale.order"},
            output=raw,
        )
        replayed, issued = await enrich_and_remember(
            db,
            tenant_id=tenant,
            scope="task:1",
            ctx=_odoo(),
            tool_name="search_records",
            arguments={"model": "sale.order"},
            output=f"{raw}\n\n{REPLAY_NOTE}",
        )
    assert replayed == f"{first}\n\n{REPLAY_NOTE}"
    assert issued == ["https://odoo.example.com/odoo/sale.order/42"]


async def test_enrich_and_remember_without_context_changes_nothing(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assert await enrich_and_remember(
            db,
            tenant_id=tenant,
            scope="task:1",
            ctx=None,
            tool_name="x",
            arguments={},
            output='[{"id": 1}]',
        ) == ('[{"id": 1}]', [])


def test_a_native_link_on_a_foreign_host_is_ignored() -> None:
    ctx = link_context_from(GITHUB, config={"focus_spec": GITHUB_FOCUS})
    item = {"number": 7, "html_url": "https://evil.example/x"}
    url = link_for(ctx, entity="issue", ref="7", args={"owner": "o", "repo": "r"}, item=item)
    assert url == "https://github.com/o/r/issues/7"
    assert link_for(ctx, entity="issue", ref="7", item=item) is None


def test_a_native_link_must_match_the_base_url_host() -> None:
    spec = RecordUrlTemplate(
        template="{base_url}/{id}",
        base_url_path=["env", "GITEA_URL"],
        models={"issue": "issues"},
        native_url_fields=["html_url"],
    )
    ctx = link_context_from(spec, config={}, env={"GITEA_URL": "https://git.acme.dev"})
    ok = {"html_url": "https://git.acme.dev/a/b/issues/3"}
    bad = {"html_url": "https://git.evil.dev/a/b/issues/3"}
    assert link_for(ctx, entity="issue", ref="3", item=ok) == ok["html_url"]
    assert link_for(ctx, entity="issue", ref="3", item=bad) == "https://git.acme.dev/3"


def test_suffix_hosts_match_subdomains_only() -> None:
    spec = RecordUrlTemplate(native_url_fields=["webUrl"], native_url_hosts=["*.sharepoint.com"])
    ctx = link_context_from(spec, config={})
    assert link_for(ctx, entity="", ref="", item={"webUrl": "https://a.sharepoint.com/x"})
    assert not link_for(ctx, entity="", ref="", item={"webUrl": "https://sharepoint.com/x"})
    assert not link_for(ctx, entity="", ref="", item={"webUrl": "https://evilsharepoint.com/x"})


def test_a_host_path_entry_matches_only_under_its_prefix() -> None:
    spec = RecordUrlTemplate(native_url_fields=["u"], native_url_hosts=["www.google.com/calendar/"])
    ctx = link_context_from(spec, config={})

    def link(url: str) -> str:
        return link_for(ctx, entity="", ref="", item={"u": url})

    assert link("https://www.google.com/calendar/event?eid=abc")
    assert not link("https://www.google.com/url?q=https://evil.example")
    assert not link("https://www.google.com/calendar/../url?q=x")
    assert not link("https://www.google.com/%63alendar/../url")
    assert not link("https://www.google.com/calendar/..\\url?q=https://evil.example")
    assert not link("https://www.google.com\\calendar\\..\\url")
    assert not link("https://www.google.com/calendarx/event")
    assert not link("https://evil.example/calendar/event")


def test_a_native_host_entry_must_start_with_a_host() -> None:
    with pytest.raises(ValueError, match="must start with a host"):
        RecordUrlTemplate(native_url_fields=["u"], native_url_hosts=["/calendar/"])


def test_native_url_fields_need_a_host_bound() -> None:
    with pytest.raises(ValueError, match="native_url_hosts or a base_url_path"):
        RecordUrlTemplate(native_url_fields=["html_url"])


def test_the_manifest_default_is_the_last_config_fallback() -> None:
    spec = RecordUrlTemplate(
        template="{base_url}/{id}", base_url_path=["env", "UI"], models={"x": "x"}
    )
    defaults = {"env": {"UI": "https://app.example.com"}}
    ctx = link_context_from(spec, config={"env": {}}, env={}, defaults=defaults)
    assert link_for(ctx, entity="x", ref="1") == "https://app.example.com/1"
    ctx = link_context_from(spec, config={"env": {"UI": "https://own.example"}}, defaults=defaults)
    assert link_for(ctx, entity="x", ref="1") == "https://own.example/1"
