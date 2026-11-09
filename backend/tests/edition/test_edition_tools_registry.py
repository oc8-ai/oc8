"""The edition-tool seam (oc8 Chat §6.2): registration, gating and the
untrusted parts an edition tool hands back."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

import pytest
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient

from oc8.agent.control_tools import CONTROL_TOOL_NAMES, ControlOutcome
from oc8.edition import EditionExtension
from oc8.edition.runtime import RuntimeComposition
from oc8.edition.tools import (
    MAX_PARTS,
    MAX_PARTS_BYTES,
    EditionTool,
    EditionToolContext,
    clean_parts,
    edition_tool_entitled,
    edition_tool_writes,
    offered_edition_tools,
    registered_edition_tool,
    use_edition_tools,
)
from oc8.licensing import ALL_CAPABILITIES, CommunityEntitlements
from oc8.licensing.capabilities import ENTERPRISE_CANVAS, ENTERPRISE_WEB_SEARCH
from oc8.main import create_app
from oc8.modelrouter import NeutralTool
from oc8.runtime.supervision_hook import NOOP_SUPERVISION_RUN_HOOK

pytestmark = pytest.mark.asyncio


@dataclass(frozen=True)
class _Granted:
    caps: frozenset[str]

    def has(self, capability: str) -> bool:
        return capability in self.caps

    def limit(self, name: str) -> int | None:
        return None


async def _noop(ctx: EditionToolContext) -> ControlOutcome:
    return ControlOutcome(output="ok", parts=[{"type": "sources", "items": []}])


SEARCH = EditionTool(
    tool=NeutralTool(
        name="web_search",
        description="Search the web.",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
    ),
    execute=_noop,
    capability=ENTERPRISE_WEB_SEARCH,
    writes=False,
    door_ok=frozenset({"web"}),
)

CANVAS = EditionTool(
    tool=NeutralTool(name="canvas_write", description="Write a canvas.", parameters={}),
    execute=_noop,
    capability=ENTERPRISE_CANVAS,
    writes=True,
)


async def _community(_tenant: uuid.UUID) -> CommunityEntitlements:
    return CommunityEntitlements()


def _granting(*caps: str) -> object:
    async def granted(_tenant: uuid.UUID) -> _Granted:
        return _Granted(frozenset(caps))

    return granted


def test_the_new_capabilities_are_known() -> None:
    for cap in (
        "enterprise.web_search",
        "enterprise.canvas",
        "enterprise.data_analysis",
        "enterprise.chat_search",
        "enterprise.chat_sharing",
        "enterprise.dictation",
        "enterprise.prompt_templates",
    ):
        assert cap in ALL_CAPABILITIES


def test_control_outcome_carries_no_parts_by_default() -> None:
    assert ControlOutcome(output="x").parts is None


async def test_community_offers_no_edition_tool() -> None:
    with use_edition_tools([SEARCH, CANVAS], _community):
        assert await offered_edition_tools(uuid.uuid4(), door="web") == []
        assert await offered_edition_tools(uuid.uuid4(), door=None) == []
        assert await edition_tool_entitled(uuid.uuid4(), SEARCH) is False
        assert registered_edition_tool("web_search") is SEARCH
    assert registered_edition_tool("web_search") is None


async def test_nothing_is_offered_without_registration() -> None:
    assert await offered_edition_tools(uuid.uuid4(), door="web") == []
    assert registered_edition_tool("web_search") is None


async def test_entitled_tenant_gets_it_on_allowed_doors_only() -> None:
    with use_edition_tools([SEARCH], _granting(ENTERPRISE_WEB_SEARCH)):  # type: ignore[arg-type]
        offered = await offered_edition_tools(uuid.uuid4(), door="web")
        assert [t.tool.name for t in offered] == ["web_search"]
        assert await offered_edition_tools(uuid.uuid4(), door="telegram") == []
        assert await offered_edition_tools(uuid.uuid4(), door=None) == []
        assert await edition_tool_entitled(uuid.uuid4(), SEARCH) is True
        assert edition_tool_writes("web_search") is False
    assert edition_tool_writes("web_search") is None


async def test_each_tool_needs_its_own_capability() -> None:
    with use_edition_tools([SEARCH, CANVAS], _granting(ENTERPRISE_CANVAS)):  # type: ignore[arg-type]
        offered = await offered_edition_tools(uuid.uuid4(), door="web")
        assert [t.tool.name for t in offered] == ["canvas_write"]
        assert edition_tool_writes("canvas_write") is True


async def test_an_agent_tool_of_the_same_name_wins() -> None:
    """An MCP tool is the agent's own; an edition tool never shadows it."""
    with use_edition_tools([SEARCH], _granting(ENTERPRISE_WEB_SEARCH)):  # type: ignore[arg-type]
        offered = await offered_edition_tools(uuid.uuid4(), door="web", taken={"web_search"})
        assert offered == []


def test_an_edition_tool_cannot_shadow_a_control_tool() -> None:
    clash = EditionTool(
        tool=NeutralTool(name="ask_user", description="x", parameters={}),
        execute=_noop,
        capability=ENTERPRISE_WEB_SEARCH,
        writes=False,
    )
    assert "ask_user" in CONTROL_TOOL_NAMES
    with pytest.raises(ValueError, match="control tool"):
        with use_edition_tools([clash], _community):
            pass


def test_duplicate_edition_tools_are_refused() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        with use_edition_tools([SEARCH, SEARCH], _community):
            pass


class _ClashingEdition(EditionExtension):
    def routers(self) -> list[object]:  # type: ignore[override]
        return []

    def tools(self) -> list[EditionTool]:
        return [
            EditionTool(
                tool=NeutralTool(name="read_context", description="x", parameters={}),
                execute=_noop,
                capability=ENTERPRISE_WEB_SEARCH,
                writes=False,
            )
        ]


class _ToolEdition(EditionExtension):
    def routers(self) -> list[APIRouter]:
        return []

    def service_routers(self) -> list[APIRouter]:
        router = APIRouter()

        @router.get("/edition-tool-probe")
        async def probe() -> dict[str, bool]:
            return {"registered": registered_edition_tool("web_search") is SEARCH}

        return [router]

    def tools(self) -> list[EditionTool]:
        return [SEARCH]


async def test_create_app_activates_extension_tools_per_request() -> None:
    app = create_app(edition_extensions=[_ToolEdition()])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/edition-tool-probe")
    assert response.json() == {"registered": True}
    assert registered_edition_tool("web_search") is None


def test_create_app_refuses_a_shadowing_edition_tool_at_startup() -> None:
    with pytest.raises(ValueError, match=r"read_context.*control tool"):
        create_app(edition_extensions=[_ClashingEdition()])


def test_a_composition_refuses_a_shadowing_tool_when_built() -> None:
    clash = EditionTool(
        tool=NeutralTool(name="suggest_replies", description="x", parameters={}),
        execute=_noop,
        capability=ENTERPRISE_WEB_SEARCH,
        writes=False,
    )
    with pytest.raises(ValueError, match="control tool"):
        RuntimeComposition(NOOP_SUPERVISION_RUN_HOOK, edition_tools=[clash])


async def test_a_composition_activates_its_tools_and_entitlements() -> None:
    composition = RuntimeComposition(
        NOOP_SUPERVISION_RUN_HOOK,
        edition_tools=[SEARCH],
        tenant_entitlements=_granting(ENTERPRISE_WEB_SEARCH),  # type: ignore[arg-type]
    ).with_tools([CANVAS])
    with composition.activate():
        offered = await offered_edition_tools(uuid.uuid4(), door="web")
        assert sorted(t.tool.name for t in offered) == ["web_search"]
        assert registered_edition_tool("canvas_write") is CANVAS
    assert registered_edition_tool("web_search") is None


async def test_the_community_composition_offers_nothing() -> None:
    from oc8.edition.runtime import COMMUNITY_RUNTIME_COMPOSITION

    with COMMUNITY_RUNTIME_COMPOSITION.activate():
        assert await offered_edition_tools(uuid.uuid4(), door="web") == []


def test_clean_parts_keeps_typed_json_objects() -> None:
    parts = [{"type": "sources", "items": [{"url": "https://x", "n": 1.5, "ok": True, "z": None}]}]
    assert clean_parts(parts) == parts


def test_clean_parts_drops_malformed_parts(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    raw: list[object] = [
        "text",
        {"items": []},
        {"type": 3},
        {"type": ""},
        {"type": "sources", "bad": object()},
        {"type": "sources", "bad": float("nan")},
        {"type": "suggestions", "options": ["spoof"]},
        {"type": "answers", "questions": []},
        {"type": "document_ref", "id": "d1", "tuple": (1, 2)},
    ]
    assert clean_parts(raw) == [{"type": "document_ref", "id": "d1", "tuple": [1, 2]}]
    assert "dropped" in caplog.text
    assert clean_parts(None) == []
    assert clean_parts({"type": "sources"}) == []


def test_clean_parts_caps_count_and_size(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    many = [{"type": "ref", "i": i} for i in range(MAX_PARTS + 5)]
    assert clean_parts(many) == many[:MAX_PARTS]

    big = {"type": "blob", "data": "x" * (MAX_PARTS_BYTES // 2)}
    small = {"type": "ref", "id": "1"}
    assert clean_parts([big, big, small]) == [big, small]
    assert clean_parts([{"type": "blob", "data": "x" * MAX_PARTS_BYTES}]) == []
    assert "dropped" in caplog.text
