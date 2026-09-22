"""An agent with two connected systems must never call the wrong one.

The failure this prevents is not a crash: it is a write that succeeds against
the wrong software. Two servers both offering `create_issue` and a model calling
the bare name would reach whichever the core looked at first.
"""

from __future__ import annotations

import pytest

from oc8.agent.tool_routing import Route, build_routes, qualified, resolve


def test_a_unique_name_stays_bare() -> None:
    """Every mission, skill and plugin in the repo names tools bare. Qualifying
    them wholesale to fix a collision that does not exist would break all of
    them."""
    routes = build_routes({"odoo": ["post_message", "get_record"], "gitea": ["create_issue"]})
    assert routes["post_message"] == Route("odoo", "post_message")
    assert routes["create_issue"] == Route("gitea", "create_issue")


def test_a_colliding_name_is_only_offered_qualified() -> None:
    """The bare form must disappear, not merely lose. If it stayed, the model
    could still express the ambiguous call -- and something would have to guess
    which system it meant."""
    routes = build_routes({"odoo": ["create_issue"], "gitea": ["create_issue"]})
    assert "create_issue" not in routes
    assert routes["odoo.create_issue"] == Route("odoo", "create_issue")
    assert routes["gitea.create_issue"] == Route("gitea", "create_issue")


def test_a_collision_does_not_qualify_the_neighbours() -> None:
    routes = build_routes({"odoo": ["create_issue", "post_message"], "gitea": ["create_issue"]})
    assert routes["post_message"] == Route("odoo", "post_message")


def test_the_result_does_not_depend_on_connection_order() -> None:
    a = build_routes({"odoo": ["x"], "gitea": ["x", "y"]})
    b = build_routes({"gitea": ["x", "y"], "odoo": ["x"]})
    assert a == b


def test_one_connection_behaves_exactly_as_before() -> None:
    routes = build_routes({"odoo": ["post_message", "get_record"]})
    assert set(routes) == {"post_message", "get_record"}


def test_an_unknown_name_has_no_route() -> None:
    assert resolve("nonsense", build_routes({"odoo": ["x"]})) is None


def test_a_needlessly_qualified_name_still_resolves() -> None:
    """Being stricter than necessary costs the model a turn and teaches it
    nothing; the call is unambiguous either way."""
    routes = build_routes({"odoo": ["post_message"]})
    assert resolve("odoo.post_message", routes) == Route("odoo", "post_message")


def test_a_qualified_name_for_the_wrong_connection_does_not_resolve() -> None:
    """The whole point: naming another system's connection must not reach this
    one just because the tool name happens to match."""
    routes = build_routes({"odoo": ["post_message"]})
    assert resolve("gitea.post_message", routes) is None


def test_qualified_is_what_build_routes_advertises() -> None:
    routes = build_routes({"a": ["t"], "b": ["t"]})
    assert qualified("a", "t") in routes


class _FakeSession:
    def __init__(self, command: str) -> None:
        self.command = command
        self.called: list[tuple[str, dict[str, object]]] = []

    async def call(self, name: str, arguments: dict[str, object]) -> str:
        self.called.append((name, arguments))
        return f"{self.command}:{name}"


@pytest.mark.asyncio
async def test_routed_toolset_dispatches_each_name_to_its_owner() -> None:
    from oc8.agent.tool_routing import RoutedToolset
    from oc8.modelrouter.types import NeutralTool

    odoo = _FakeSession("odoo")
    gitea = _FakeSession("gitea")
    routed = RoutedToolset(
        {"odoo": odoo, "gitea": gitea},
        {
            "odoo": [NeutralTool(name="search_records", description="", parameters={})],
            "gitea": [NeutralTool(name="create_issue", description="", parameters={})],
        },
    )
    names = {t.name for t in routed.tools}
    assert names == {"search_records", "create_issue"}
    assert await routed.call("create_issue", {"title": "x"}) == "gitea:create_issue"
    assert await routed.call("search_records", {}) == "odoo:search_records"
    assert odoo.called == [("search_records", {})]
    assert gitea.called == [("create_issue", {"title": "x"})]


@pytest.mark.asyncio
async def test_routed_toolset_qualifies_a_collision() -> None:
    from oc8.agent.tool_routing import RoutedToolset
    from oc8.modelrouter.types import NeutralTool

    odoo = _FakeSession("odoo")
    gitea = _FakeSession("gitea")
    tool = NeutralTool(name="create_issue", description="", parameters={})
    routed = RoutedToolset(
        {"odoo": odoo, "gitea": gitea},
        {"odoo": [tool], "gitea": [tool]},
    )
    names = {t.name for t in routed.tools}
    assert "create_issue" not in names
    assert names == {"odoo.create_issue", "gitea.create_issue"}
    assert await routed.call("gitea.create_issue", {}) == "gitea:create_issue"
    assert odoo.called == []
    assert gitea.called == [("create_issue", {})]
