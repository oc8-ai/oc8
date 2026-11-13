"""Every capa's real `record_url` declaration, run through the real
`enrich` (spec 2026-11-13-record-links-design.md §4.2). Editing a capa's
tool_pack.toml without editing this must fail: each case is a link an
approver or a chat reader will click."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from oc8.capas.discovery import find_plugin
from oc8.capas.manifest import ToolPackConnection, parse_manifest
from oc8.records.links import LinkContext, enrich, link_context_from, related_links

# tests/plugins/<this> -> tests -> backend -> repo root, where capas/ lives.
_PLUGINS_DIR = Path(__file__).resolve().parents[3] / "capas"


def _connection(plugin: str) -> ToolPackConnection:
    found = find_plugin(plugin, [str(_PLUGINS_DIR)])
    assert found is not None, f"{plugin} plugin not found"
    assert found.valid, found.error
    assert found.manifest is not None
    manifest = parse_manifest(found.manifest)
    assert manifest.tool_pack is not None
    return manifest.tool_pack.connections[0]


def _ctx(plugin: str, env: dict[str, str], config_env: dict[str, str] | None = None) -> LinkContext:
    conn = _connection(plugin)
    assert conn.record_url is not None
    # `config` is the ROW's config (the pack's env defaults are not in it once a
    # login has rewritten them); `defaults` is the manifest, the last fallback.
    ctx = link_context_from(
        conn.record_url,
        config={**conn.config, "env": env if config_env is None else config_env},
        env=env,
        defaults=conn.config,
    )
    assert ctx is not None
    return ctx


_HUBSPOT = (
    "hubspot_mcp",
    "hubspot-search-objects",
    {"objectType": "deals"},
    {"results": [{"id": "77", "properties": {"dealname": "Big"}}]},
)

CASES: list[tuple[str, str, dict[str, Any], Any, dict[str, str], list[str]]] = [
    (
        "jira_mcp",
        "jira_search",
        {"jql": "x"},
        {"issues": [{"key": "ABC-1"}, {"key": "ABC-2"}]},
        {"JIRA_URL": "https://acme.atlassian.net"},
        ["https://acme.atlassian.net/browse/ABC-1", "https://acme.atlassian.net/browse/ABC-2"],
    ),
    (
        "jira_mcp",
        "jira_get_issue",
        {"issue_key": "ABC-1"},
        {"key": "ABC-1", "fields": {}},
        {"JIRA_URL": "https://acme.atlassian.net"},
        ["https://acme.atlassian.net/browse/ABC-1"],
    ),
    (
        "github_mcp",
        "issue_read",
        {"owner": "oc8-ai", "repo": "oc8", "issue_number": 5},
        {"number": 5},
        {},
        ["https://github.com/oc8-ai/oc8/issues/5"],
    ),
    (
        "github_mcp",
        "search_issues",
        {"query": "x"},
        {"items": [{"number": 9, "html_url": "https://github.com/oc8-ai/oc8/issues/9"}]},
        {},
        ["https://github.com/oc8-ai/oc8/issues/9"],
    ),
    (
        "gitea_mcp",
        "get_issue",
        {"owner": "acme", "repo": "web", "index": 3},
        {"number": 3},
        {"GITEA_URL": "https://git.acme.dev"},
        ["https://git.acme.dev/acme/web/issues/3"],
    ),
    (
        *_HUBSPOT,
        {"HUBSPOT_UI_URL": "https://app-eu1.hubspot.com", "HUBSPOT_PORTAL_ID": "1234"},
        ["https://app-eu1.hubspot.com/contacts/1234/record/0-3/77"],
    ),
    (
        "odoo_mcp",
        "search_records",
        {"model": "sale.order"},
        [{"id": 42, "name": "S00042"}],
        {"ODOO_URL": "https://odoo.acme.de"},
        ["https://odoo.acme.de/odoo/sale.order/42"],
    ),
    (
        "moodle",
        "moodle_search_courses",
        {"query": "x"},
        {"items": [{"id": 7, "fullname": "Bio"}], "total": 1, "offset": 0, "truncated": False},
        {"MOODLE_URL": "https://lms.acme.de"},
        ["https://lms.acme.de/course/view.php?id=7"],
    ),
]


@pytest.mark.parametrize(
    ("plugin", "tool", "args", "result", "env", "expected"),
    CASES,
    ids=[f"{c[0]}-{c[1]}" for c in CASES],
)
def test_capa_declares_the_expected_links(
    plugin: str,
    tool: str,
    args: dict[str, Any],
    result: Any,
    env: dict[str, str],
    expected: list[str],
) -> None:
    _, urls = enrich(_ctx(plugin, env), tool, args, json.dumps(result))
    assert urls == expected


def test_hubspot_without_portal_id_has_no_links() -> None:
    plugin, tool, args, result = _HUBSPOT
    ctx = _ctx(plugin, {"HUBSPOT_UI_URL": "https://app-eu1.hubspot.com"})
    _, urls = enrich(ctx, tool, args, json.dumps(result))
    assert urls == []


def test_odoo_related_partner_link() -> None:
    ctx = _ctx("odoo_mcp", {"ODOO_URL": "https://odoo.acme.de"})
    links = related_links(ctx, {"model": "sale.order", "values": {"partner_id": 7}})
    assert len(links) == 1
    assert "/odoo/res.partner/7" in str(links[0])


def test_hubspot_defaults_to_the_us_address_when_the_login_leaves_it_blank() -> None:
    plugin, tool, args, result = _HUBSPOT
    ctx = _ctx(plugin, {"HUBSPOT_PORTAL_ID": "1234"}, config_env={})
    _, urls = enrich(ctx, tool, args, json.dumps(result))
    assert urls == ["https://app.hubspot.com/contacts/1234/record/0-3/77"]


def test_github_ignores_a_foreign_html_url() -> None:
    ctx = _ctx("github_mcp", {})
    out = json.dumps({"number": 5, "html_url": "https://evil.example/x"})
    args = {"owner": "oc8-ai", "repo": "oc8", "issue_number": 5}
    _, urls = enrich(ctx, "issue_read", args, out)
    assert urls == ["https://github.com/oc8-ai/oc8/issues/5"]


def test_gitea_ignores_an_html_url_off_its_own_host() -> None:
    ctx = _ctx("gitea_mcp", {"GITEA_URL": "https://git.acme.dev"})
    out = json.dumps({"number": 3, "html_url": "https://evil.example/x"})
    _, urls = enrich(ctx, "get_issue", {"owner": "acme", "repo": "web", "index": 3}, out)
    assert urls == ["https://git.acme.dev/acme/web/issues/3"]
