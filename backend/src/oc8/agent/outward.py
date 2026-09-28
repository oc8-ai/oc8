"""One message per record per task, enforced by the core.

A mission rule ("write exactly one customer-visible message per run") is a
request to the model, and a model that is having a bad turn will simply call the
tool twice. That happened: one ticket, one question, two full answers to the
customer within a single run. Nothing in the loop objected, because both calls
were individually legitimate -- different bodies, so the idempotency store saw
two different calls, and a policy that only asks "may this agent send?" answers
yes both times.

What makes the second call wrong is not its content but that a person already
received the first one. So the check is on the RECIPIENT, not on the arguments:
at most one outward message per record, per task.

The core does not know which tools reach a person -- that is software-specific,
and no vendor knowledge may live here (§ core is software-neutral). A connection
declares it, the same way it declares where a call's value and its record are:

    [plugin.tool_pack.connections.config]
    outward_tools = ["post_message"]

An empty declaration leaves the guard inert, so nothing changes for a connection
that has not thought about it.

LIMIT, deliberately: a call is identified by its record, not by its channel. A
connection whose single tool writes both an internal note and a customer-visible
reply cannot express "one reply, plus notes" -- it either declares the tool
outward (one call per record) or it does not. Splitting that needs a channel
discriminator in the spec, and there is no case for one yet.

Task-scoped, not run-scoped: a task suspended for an approval and resumed later
is the same piece of work, and the customer does not care that the second half
ran in a different container.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oc8.agent.tool_idempotency import record_invocation, replayed_result
from oc8.agent.tool_semantics import call_entity, focus_ref_id

#: What the model is told when the second message is refused. It has to say what
#: already happened, or the model reads a bare denial as a transport failure and
#: tries again with a reworded body.
REFUSAL = (
    "ERROR: a message was already sent to {target} in this task. A second one "
    "would reach the recipient as a duplicate answer to a single question, so "
    "it was not sent. Do not rephrase and retry. If something still needs "
    "saying, it belongs in the next run, after the recipient has replied."
)


def is_outward_skipped(
    tool_name: str,
    arguments: dict[str, Any],
    skip_spec: dict[str, Any] | None,
) -> bool:
    """True when the connection's outward_skip_spec says this call reaches nobody.

    Used for the note/comment split on a single tool (e.g. Odoo post_message
    with subtype=note). Declared on the connection; the core stays software-
    neutral and only matches argument values against the declared lists.
    """
    if not skip_spec:
        return False
    rules = skip_spec.get(tool_name)
    if not isinstance(rules, dict):
        return False
    for key, allowed in rules.items():
        if not isinstance(allowed, list):
            continue
        if arguments.get(key) in allowed:
            return True
    return False


def outward_target(
    tool_name: str,
    arguments: dict[str, Any],
    focus_spec: dict[str, Any] | None,
    outward_tools: list[str] | None,
    *,
    skip_spec: dict[str, Any] | None = None,
) -> str | None:
    """Who this call would reach, or None when it reaches nobody.

    None means "not an outward call at all" -- the overwhelmingly common case,
    and the one that must stay free of any cost.
    """
    if not outward_tools or tool_name not in outward_tools:
        return None
    if is_outward_skipped(tool_name, arguments, skip_spec):
        return None
    spec = focus_spec or {}
    # Both declared shapes, not just `entity_field`: software with one endpoint
    # per KIND of thing (`calendar_create_event`) names the entity in the tool,
    # never in an argument, so reading only the argument left every such call
    # sharing one target -- i.e. one create per task, with no way to express a
    # second. `tool_entities` is how the spec already says that elsewhere.
    entity = call_entity(tool_name, arguments, spec).strip()
    ref = focus_ref_id(arguments, spec)
    if entity and ref:
        return f"{entity}#{ref}"
    # No record named: fall back to the tool itself, which still stops a run
    # from firing the same broadcast twice. Coarser on purpose -- a guard that
    # gives up when it cannot identify the recipient guards nothing.
    return f"{entity or tool_name}#"


def _marker(target: str) -> str:
    return f"outward:{target}"


async def already_delivered(
    db: AsyncSession, *, tenant_id: uuid.UUID, task_id: uuid.UUID, target: str
) -> bool:
    return (
        await replayed_result(
            db, tenant_id=tenant_id, task_id=task_id, tool=_marker(target), arguments={}
        )
    ) is not None


async def remember_delivery(
    db: AsyncSession, *, tenant_id: uuid.UUID, task_id: uuid.UUID, target: str
) -> None:
    """Record that a person has been reached. Only ever after a call SUCCEEDED --
    remembering a failed send would silence the retry that should happen."""
    await record_invocation(
        db,
        tenant_id=tenant_id,
        task_id=task_id,
        tool=_marker(target),
        arguments={},
        result=target,
    )
