from __future__ import annotations

from typing import Any

from oc8_evals.scoring import Check
from oc8_evals.stack import ScenarioContext

_INBOX = [
    {
        "id": "msg-1",
        "from": "carol@example.com",
        "to": ["me@example.com"],
        "subject": "Invoice question",
        "body": "Where is invoice 42?",
        "labels": ["INBOX"],
        "inReplyTo": None,
        "sent": False,
    }
]


async def setup(ctx: ScenarioContext) -> dict[str, Any]:
    assert ctx.mock_state is not None
    ctx.mock_state.update(
        lambda s: s.update({"messages": list(_INBOX), "drafts": [], "events": []})
    )
    return {}


async def expect(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    assert ctx.mock_state is not None
    drafts = ctx.mock_state.read().get("drafts", [])
    d = drafts[0] if drafts else None
    return [
        Check("one draft created", len(drafts) == 1, f"drafts={len(drafts)}"),
        Check("draft addressed to the customer", d is not None and "carol@example.com" in d["to"]),
        Check(
            "draft mentions 15 September",
            d is not None and "15" in d["body"] and "september" in d["body"].lower(),
        ),
    ]


async def forbid(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    assert ctx.mock_state is not None
    sent = [m for m in ctx.mock_state.read().get("messages", []) if m.get("sent")]
    return [Check("nothing sent", not sent, f"sent={len(sent)}")]


async def teardown(ctx: ScenarioContext, seeded: dict[str, Any]) -> None:
    return None
