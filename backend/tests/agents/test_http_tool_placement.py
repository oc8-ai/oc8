"""Where each argument goes: the URL path, the query string, a header or the
JSON body. Without `param_locations` today's rule holds: GET/DELETE/HEAD put
everything in the query, every other method in the body."""

from __future__ import annotations

import pytest

from oc8.agent.http_tools import place_arguments

_PROTECTED = frozenset({"authorization", "x-api-key"})


def _tool(method: str, template: str, **extra: object) -> dict[str, object]:
    return {"name": "t", "method": method, "url_template": template, **extra}


def test_legacy_get_puts_everything_in_the_query() -> None:
    placed = place_arguments(
        _tool("GET", "/users/{id}"), {"id": "4/2", "q": "ada"}, protected_headers=_PROTECTED
    )
    assert placed.path == "/users/4%2F2"
    assert placed.query == {"q": "ada"}
    assert placed.has_body is False


def test_legacy_delete_uses_the_query() -> None:
    placed = place_arguments(_tool("DELETE", "/x"), {"force": True}, protected_headers=_PROTECTED)
    assert placed.query == {"force": True}
    assert placed.has_body is False


def test_legacy_post_sends_the_rest_as_body_even_when_empty() -> None:
    placed = place_arguments(_tool("POST", "/x"), {}, protected_headers=_PROTECTED)
    assert placed.body == {}
    assert placed.has_body is True


def test_declared_locations_win() -> None:
    tool = _tool(
        "POST",
        "/orgs/{org}/tickets",
        param_locations={"org": "path", "page": "query", "X-Trace": "header", "title": "body"},
    )
    placed = place_arguments(
        tool,
        {"org": "acme", "page": 2, "X-Trace": "abc", "title": "Printer"},
        protected_headers=_PROTECTED,
    )
    assert placed.path == "/orgs/acme/tickets"
    assert placed.query == {"page": 2}
    assert placed.headers == {"X-Trace": "abc"}
    assert placed.body == {"title": "Printer"}


def test_body_param_is_the_whole_body() -> None:
    tool = _tool("PUT", "/items/{id}", param_locations={"id": "path"}, body_param="item")
    placed = place_arguments(tool, {"id": "7", "item": [1, 2, 3]}, protected_headers=_PROTECTED)
    assert placed.body == [1, 2, 3]
    assert placed.has_body is True


def test_body_param_object_merges_other_body_fields() -> None:
    tool = _tool("POST", "/x", param_locations={"note": "body"}, body_param="payload")
    placed = place_arguments(
        tool, {"payload": {"a": 1}, "note": "hi"}, protected_headers=_PROTECTED
    )
    assert placed.body == {"a": 1, "note": "hi"}


@pytest.mark.parametrize("name", ["Authorization", "X-API-KEY", "Host", "Cookie"])
def test_a_header_parameter_never_overrides_auth_or_transport_headers(name: str) -> None:
    tool = _tool("GET", "/x", param_locations={name: "header"})
    placed = place_arguments(tool, {name: "evil"}, protected_headers=_PROTECTED)
    assert placed.headers == {}


def test_unlisted_arguments_fall_back_to_the_method_rule() -> None:
    tool = _tool("GET", "/x", param_locations={"a": "header"})
    placed = place_arguments(tool, {"a": "1", "b": "2"}, protected_headers=_PROTECTED)
    assert placed.headers == {"a": "1"}
    assert placed.query == {"b": "2"}


def test_structured_query_values_are_sent_as_json() -> None:
    placed = place_arguments(
        _tool("GET", "/x"),
        {"filter": {"state": "open"}, "ids": [1, 2]},
        protected_headers=_PROTECTED,
    )
    assert placed.query == {"filter": '{"state": "open"}', "ids": [1, 2]}


def test_missing_path_parameter() -> None:
    with pytest.raises(RuntimeError, match="missing required parameter"):
        place_arguments(_tool("GET", "/users/{id}"), {}, protected_headers=_PROTECTED)
