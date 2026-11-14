"""Where each argument goes: the URL path, the query string, a header or the
JSON body. Without `param_locations` today's rule holds: GET/DELETE/HEAD put
everything in the query, every other method in the body."""

from __future__ import annotations

import pytest

from oc8.agent.http_tools import HttpToolError, place_arguments

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


@pytest.mark.parametrize("template", ["/users/{id}", "/users/{id}/orders"])
@pytest.mark.parametrize(("value", "encoded"), [("..", "%2E%2E"), (".", "%2E"), ("../x", "..%2Fx")])
def test_dot_segments_are_encoded_in_the_path(template: str, value: str, encoded: str) -> None:
    import httpx

    placed = place_arguments(_tool("GET", template), {"id": value}, protected_headers=_PROTECTED)
    assert encoded in placed.path
    url = httpx.Request("GET", "https://api.example.com/v1" + placed.path).url
    assert url.path.startswith("/v1/users/")


def test_path_values_are_serialised_like_query_values() -> None:
    tool = _tool("GET", "/a/{flag}/{ids}/{obj}")
    placed = place_arguments(
        tool, {"flag": True, "ids": [1, 2], "obj": {"k": 1}}, protected_headers=_PROTECTED
    )
    assert placed.path == "/a/true/%5B1%2C%202%5D/%7B%22k%22%3A%201%7D"


def test_none_path_value_is_an_error_and_none_query_value_is_omitted() -> None:
    with pytest.raises(RuntimeError, match="missing required parameter"):
        place_arguments(_tool("GET", "/u/{id}"), {"id": None}, protected_headers=_PROTECTED)
    placed = place_arguments(_tool("GET", "/u"), {"q": None}, protected_headers=_PROTECTED)
    assert placed.query == {}


@pytest.mark.parametrize("value", ["a\r\nX-Evil: 1", "a\nb", "a\x00b"])
def test_header_values_with_control_characters_are_rejected(value: str) -> None:
    tool = _tool("GET", "/x", param_locations={"X-T": "header"})
    with pytest.raises(HttpToolError):
        place_arguments(tool, {"X-T": value}, protected_headers=_PROTECTED)


@pytest.mark.parametrize("name", ["a b", "a\r\nX", "a:b", ""])
def test_invalid_header_names_are_rejected(name: str) -> None:
    tool = _tool("GET", "/x", param_locations={name: "header"})
    with pytest.raises(HttpToolError):
        place_arguments(tool, {name: "v"}, protected_headers=_PROTECTED)


def test_numeric_placeholder_does_not_break_the_template() -> None:
    placed = place_arguments(_tool("GET", "/a/{0}"), {"0": "x"}, protected_headers=_PROTECTED)
    assert placed.path == "/a/x"
