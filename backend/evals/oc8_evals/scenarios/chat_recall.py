"""chat_harness / recall: a fact from early in a long chat, asked for after the
chat outgrew its window (spec §5.3 checkpoints).

`setup` seeds the chat: `params.early` statements (each acknowledged), then
`params.filler_turns` exchanges of office small talk, then `params.late`. The
task's small `context_window_tokens` makes the replay overflow, so the run
summarises the start into a checkpoint before it answers. Scored on state: the
checkpoint row (and that it covers the facts), the stored answer.

`params.checkpoint`:
- "made": the run (or the reply before it) must have made a checkpoint whose
  cut lies after the last early statement -- the facts reach the model only
  through the summary.
- "seeded": the facts live only in a checkpoint `setup` writes; the messages
  they came from are filler. Measures replay from a checkpoint.
- "none": a short chat; no checkpoint may be made.
"""

from __future__ import annotations

from typing import Any

from oc8 import models as m
from oc8.chat.checkpoint import SUMMARY_TEMPLATE
from oc8.db.session import tenant_session
from oc8_evals.scenarios import _chat
from oc8_evals.scoring import Check
from oc8_evals.stack import ScenarioContext

_ACK = "Noted, thanks."


def _exchange(n: int) -> list[tuple[str, str]]:
    return [
        ("user", _chat.filler(n)),
        ("assistant", "Thanks for the update. " + _chat.filler(n + 7, 4)),
    ]


async def setup(ctx: ScenarioContext) -> dict[str, Any]:
    params = ctx.task.params
    mode = str(params.get("checkpoint", "made"))
    early = [str(s) for s in params.get("early", [])]
    late = [str(s) for s in params.get("late", [])]
    filler = int(params.get("filler_turns", 0))
    seeded: dict[str, Any] = {"mode": mode}
    if mode == "seeded":
        before = [row for n in range(filler) for row in _exchange(n)]
        made = await _chat.seed_turns(ctx, before)
        async with tenant_session(ctx.tenant_id) as db:
            db.add(
                m.ChatCheckpoint(
                    tenant_id=ctx.tenant_id,
                    session_id=_chat.chat(ctx).session_id,
                    upto_message_id=made[-1].id,
                    summary=_summary(early),
                    token_estimate=200,
                    model="eval-seeded",
                    restricted=False,
                )
            )
        after = [row for n in range(2) for row in _exchange(filler + n)]
        await _chat.seed_turns(ctx, [*after, *_statements(late)])
        return seeded
    rows = [*_statements(early)]
    rows += [row for n in range(filler) for row in _exchange(n)]
    rows += _statements(late)
    made = await _chat.seed_turns(ctx, rows)
    # The last early statement's reply: a checkpoint covering the facts cuts
    # at or after it.
    seeded["order"] = [str(msg.id) for msg in made]
    seeded["last_fact"] = str(made[2 * len(early) - 1].id) if early else None
    return seeded


def _statements(lines: list[str]) -> list[tuple[str, str]]:
    return [row for line in lines for row in (("user", line), ("assistant", _ACK))]


def _summary(facts: list[str]) -> str:
    """A checkpoint written the way the chat's own summary is laid out."""
    headings = SUMMARY_TEMPLATE.split("\n")[1:]
    body = {
        "Facts, files and objects referenced (with ids)": "\n".join(f"- {f}" for f in facts),
    }
    return "\n".join(f"{h}\n{body.get(h, '- none')}" for h in headings)


async def expect(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    text = await _chat.reply_text(ctx)
    params = ctx.task.params
    found, _ = _chat.reply_checks(text, expect=list(params.get("expect", [])), forbid=[])
    checks = [_chat.answered(ctx, text), *found]
    rows = await _chat.checkpoints_of(ctx)
    mode = seeded.get("mode")
    if mode == "made":
        order: list[str] = seeded["order"]
        covering = [
            c
            for c in rows
            if str(c.upto_message_id) in order
            and seeded["last_fact"] is not None
            and order.index(str(c.upto_message_id)) >= order.index(seeded["last_fact"])
        ]
        checks.append(
            Check(
                "a checkpoint was made that covers the early facts",
                bool(covering),
                f"checkpoints={len(rows)} covering={len(covering)}",
            )
        )
    elif mode == "none":
        checks.append(Check("no checkpoint for a short chat", not rows, f"checkpoints={len(rows)}"))
    return checks


async def forbid(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    text = await _chat.reply_text(ctx)
    _, bad = _chat.reply_checks(text, expect=[], forbid=list(ctx.task.params.get("forbid", [])))
    return bad


async def teardown(ctx: ScenarioContext, seeded: dict[str, Any]) -> None:
    return None
