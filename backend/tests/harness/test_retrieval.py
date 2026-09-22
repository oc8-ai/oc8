"""A4: select a core tool set inline and rank the rest with BM25."""

from __future__ import annotations

from oc8.agent.harness.retrieval import ToolCard, rank_tools, select_inline

_FIND_TOOLS_DESC = (
    "Search tools that are not in your current list. "
    "Returns up to 10 matches; they are available on the next step."
)


def _card(
    name: str,
    *,
    connection: str = "oc8",
    description: str = "",
    notes: str = "",
) -> ToolCard:
    return ToolCard(
        name=name,
        description=description or f"Tool {name}",
        connection=connection,
        notes=notes,
    )


def _catalog(n: int) -> list[ToolCard]:
    return [_card(f"tool_{i:02d}") for i in range(n)]


def test_at_threshold_offers_everything_without_find_tools() -> None:
    catalog = _catalog(30)
    inline, deferred = select_inline(
        catalog,
        control_names=frozenset(),
        skill_names=frozenset(),
        mission="",
        skill_texts=[],
        procedure_texts=[],
        pinned=[],
        tool_list_may_change=True,
    )
    assert inline == catalog
    assert deferred == []
    assert all(c.name != "find_tools" for c in inline)


def test_above_threshold_offers_core_plus_find_tools() -> None:
    # Catalog order: control, skill, mission-mentioned, pinned, then filler.
    control = _card("ask_human")
    skill = _card("skill_summarize")
    mission_hit = _card("send_invoice")
    pinned = _card("search_archive")
    filler = [_card(f"filler_{i:02d}") for i in range(27)]
    catalog = [control, skill, mission_hit, pinned, *filler]
    assert len(catalog) == 31

    inline, deferred = select_inline(
        catalog,
        control_names=frozenset({"ask_human"}),
        skill_names=frozenset({"skill_summarize"}),
        mission="Please send_invoice for the overdue account.",
        skill_texts=[],
        procedure_texts=[],
        pinned=["search_archive"],
        tool_list_may_change=True,
    )

    inline_names = [c.name for c in inline]
    assert inline_names == [
        "ask_human",
        "skill_summarize",
        "send_invoice",
        "search_archive",
        "find_tools",
    ]
    find_card = inline[-1]
    assert find_card == ToolCard(
        name="find_tools",
        description=_FIND_TOOLS_DESC,
        connection="oc8",
        notes="",
    )
    assert [c.name for c in deferred] == [c.name for c in filler]
    assert len(deferred) == 27


def test_tool_list_may_change_false_skips_deferral() -> None:
    catalog = _catalog(31)
    inline, deferred = select_inline(
        catalog,
        control_names=frozenset(),
        skill_names=frozenset(),
        mission="",
        skill_texts=[],
        procedure_texts=[],
        pinned=[],
        tool_list_may_change=False,
    )
    assert inline == catalog
    assert deferred == []
    assert all(c.name != "find_tools" for c in inline)


def test_bm25_ranks_unique_notes_token_first() -> None:
    unique = _card("alpha", notes="contains the raretoken once")
    distractors = [
        _card("beta", description="ordinary helper"),
        _card("gamma", description="another ordinary helper"),
    ]
    ranked = rank_tools([*distractors, unique], "raretoken", connection=None)
    assert ranked[0].name == "alpha"
    # Default keeps top-N even when later cards score zero.
    assert len(ranked) == 3


def test_require_match_drops_zero_score_cards() -> None:
    unique = _card("alpha", notes="contains the raretoken once")
    distractors = [
        _card("beta", description="ordinary helper"),
        _card("gamma", description="another ordinary helper"),
    ]
    ranked = rank_tools(
        [*distractors, unique],
        "raretoken",
        connection=None,
        require_match=True,
    )
    assert [c.name for c in ranked] == ["alpha"]


def test_connection_filter_drops_non_matching_cards() -> None:
    mail = _card("list_inbox", connection="mail")
    other = _card("create_ticket", connection="helpdesk")
    ranked = rank_tools([mail, other], "list", connection="mail")
    assert [c.name for c in ranked] == ["list_inbox"]


def test_select_completion_tools_stores_deferred_only() -> None:
    from oc8.modelrouter import NeutralTool

    from oc8.agent.harness.retrieval import select_completion_tools

    find_tools = NeutralTool(
        name="find_tools",
        description=_FIND_TOOLS_DESC,
        parameters={"type": "object", "properties": {}},
    )
    control = NeutralTool(
        name="ask_human",
        description="Ask",
        parameters={"type": "object", "properties": {}},
    )
    fillers = [
        NeutralTool(
            name=f"filler_{i:02d}",
            description=f"Filler {i}",
            parameters={"type": "object", "properties": {}},
        )
        for i in range(30)
    ]
    offered = [control, *fillers]
    assert len(offered) == 31

    completion, catalog = select_completion_tools(
        offered,
        control_names=frozenset({"ask_human"}),
        skill_names=frozenset(),
        mission="",
        skill_texts=[],
        pinned=[],
        tool_list_may_change=True,
        mcp_connection=None,
        tool_notes=None,
        find_tools=find_tools,
    )
    assert [t.name for t in completion] == ["ask_human", "find_tools"]
    assert [c["name"] for c in catalog] == [f"filler_{i:02d}" for i in range(30)]
    assert all(c["name"] != "ask_human" for c in catalog)
    assert all(c["name"] != "find_tools" for c in catalog)


def test_select_completion_tools_empty_catalog_when_not_deferring() -> None:
    from oc8.modelrouter import NeutralTool

    from oc8.agent.harness.retrieval import select_completion_tools

    find_tools = NeutralTool(
        name="find_tools",
        description=_FIND_TOOLS_DESC,
        parameters={"type": "object", "properties": {}},
    )
    offered = [
        NeutralTool(
            name=f"tool_{i:02d}",
            description=f"Tool {i}",
            parameters={"type": "object", "properties": {}},
        )
        for i in range(10)
    ]
    completion, catalog = select_completion_tools(
        offered,
        control_names=frozenset(),
        skill_names=frozenset(),
        mission="",
        skill_texts=[],
        pinned=[],
        tool_list_may_change=True,
        mcp_connection=None,
        tool_notes=None,
        find_tools=find_tools,
    )
    assert [t.name for t in completion] == [t.name for t in offered]
    assert catalog == []
