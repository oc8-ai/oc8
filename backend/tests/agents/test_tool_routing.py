"""An agent with two connected systems must never call the wrong one.

The failure this prevents is not a crash: it is a write that succeeds against
the wrong software. Two servers both offering `create_issue` and a model calling
the bare name would reach whichever the core looked at first.
"""

from __future__ import annotations

import re

import pytest

from oc8.agent.tool_routing import (
    Route,
    addresses_connection,
    build_routes,
    qualified,
    resolve,
)

#: What every model provider accepts as a tool name (OpenAI caps at 64).
_PROVIDER_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


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
    assert routes["odoo__create_issue"] == Route("odoo", "create_issue")
    assert routes["gitea__create_issue"] == Route("gitea", "create_issue")


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
    assert resolve("odoo__post_message", routes) == Route("odoo", "post_message")


def test_a_qualified_name_for_the_wrong_connection_does_not_resolve() -> None:
    """The whole point: naming another system's connection must not reach this
    one just because the tool name happens to match."""
    routes = build_routes({"odoo": ["post_message"]})
    assert resolve("gitea__post_message", routes) is None
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
    assert names == {"odoo__create_issue", "gitea__create_issue"}
    assert await routed.call("gitea__create_issue", {}) == "gitea:create_issue"
    assert odoo.called == []
    assert gitea.called == [("create_issue", {})]


def test_a_connection_tool_named_like_a_core_tool_is_always_qualified() -> None:
    """A bare core name must always mean the core tool. A connection's own
    `ask_user` is offered as `<connection>__ask_user` even when it is the only
    one, so the model can name either, and nothing has to guess which it meant."""
    routes = build_routes({"odoo": ["ask_user", "search"]}, reserved=frozenset({"ask_user"}))
    assert "ask_user" not in routes
    assert routes[qualified("odoo", "ask_user")] == Route("odoo", "ask_user")
    assert routes["search"] == Route("odoo", "search")
    assert resolve("ask_user", routes) is None


# ------------------------------------------------ names every provider accepts


def test_every_advertised_name_is_one_every_provider_accepts() -> None:
    """A dot is refused by Anthropic, OpenAI and OpenAI-compatible gateways alike
    (`tools.N.name: String should match pattern '^[a-zA-Z0-9_-]{1,128}$'`), so
    two mail connections both offering `mail_list_folders` failed the whole run."""
    routes = build_routes(
        {
            "microsoft365": ["mail_list_folders", "mail_get"],
            "shared_mailboxes": ["mail_list_folders", "mail_get"],
            "Shared Mail (EU).v2": ["mail_list_folders"],
            "x" * 80: ["mail_get"],
        },
        reserved=frozenset({"ask_user"}),
    )
    assert len(routes) == 6
    for name in routes:
        assert _PROVIDER_NAME.match(name), name


def test_a_connection_name_with_other_characters_is_made_safe() -> None:
    routes = build_routes({"Shared Mail (EU)": ["t"], "odoo": ["t"]})
    assert routes["Shared_Mail__EU___t"] == Route("Shared Mail (EU)", "t")


def test_a_long_name_is_capped_and_stays_distinct() -> None:
    long = "c" * 70
    routes = build_routes({long + "a": ["t"], long + "b": ["t"]})
    names = sorted(routes)
    assert len(names) == 2 and names[0] != names[1]
    assert all(len(n) <= 64 for n in names)
    assert {r.connection for r in routes.values()} == {long + "a", long + "b"}


def test_two_connections_that_map_to_one_name_stay_apart() -> None:
    """Swapping characters can make two names one ("a b" and "a_b"); each still
    reaches its own system, never the neighbour's."""
    routes = build_routes({"a b": ["t"], "a_b": ["t"]})
    assert len(routes) == 2
    assert {r.connection for r in routes.values()} == {"a b", "a_b"}
    for name in routes:
        assert _PROVIDER_NAME.match(name), name


def test_a_qualified_name_never_shadows_a_bare_one() -> None:
    """A server may itself offer a tool literally called `odoo__t`."""
    routes = build_routes({"odoo": ["t"], "gitea": ["t", "odoo__t"]})
    assert routes["odoo__t"] == Route("gitea", "odoo__t")
    assert Route("odoo", "t") in routes.values()
    assert len(routes) == 3


def test_a_bare_name_a_provider_refuses_is_made_safe_too() -> None:
    routes = build_routes({"odoo": ["mail.search"]})
    assert "mail.search" not in routes
    (name,) = routes
    assert _PROVIDER_NAME.match(name)
    assert routes[name] == Route("odoo", "mail.search")


def test_the_mapping_is_the_same_on_every_run() -> None:
    offered = {"a b": ["t"], "a_b": ["t"], "z" * 90: ["t"]}
    assert build_routes(offered) == build_routes(dict(reversed(list(offered.items()))))


def test_a_dotted_name_from_a_run_already_under_way_still_resolves() -> None:
    """A run that started before the change may still call `<connection>.<tool>`."""
    routes = build_routes({"odoo": ["create_issue"], "gitea": ["create_issue"]})
    assert resolve("gitea.create_issue", routes) == Route("gitea", "create_issue")


def test_a_call_addresses_its_connection() -> None:
    assert addresses_connection("gitea__create_issue", "gitea")
    assert addresses_connection("gitea.create_issue", "gitea")
    assert addresses_connection("Shared_Mail__EU___t", "Shared Mail (EU)")
    assert not addresses_connection("odoo__create_issue", "gitea")
    assert not addresses_connection("create_issue", "gitea")
