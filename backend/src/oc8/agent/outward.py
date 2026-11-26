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
that has not thought about it. A connection may also say who a call reaches
(`focus_spec.outward_fields`: tool -> argument names holding addresses); such a
call is keyed by that set of people, whatever it says. Without it, a call that
names no record stays at one per task: fail closed.

A call a person approved through its card is the person's decision, and this
guard does not refuse it (`check_outward(approved=...)`): it stops unattended
repeats, not a human's second "yes".

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
from email.utils import getaddresses
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oc8.agent.tool_idempotency import claim_invocation, record_invocation, replayed_result
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
    # Who the call reaches, where the connection declares it: the people are
    # the target, never the wording. A reworded subject to the same person is
    # exactly the repeat this guard is for (E2E N1, security review).
    recipients = _declared_recipients(tool_name, arguments, spec)
    if recipients is None:
        return UNPARSABLE + tool_name
    if recipients:
        return RECIPIENTS + ",".join(recipients)
    if _declares_recipients(tool_name, spec):
        # Declared, but nobody named: once per task for the tool, never
        # "no key" -- and never a record's key, which a second send to an
        # unnamed recipient would not share.
        return f"{tool_name}#"
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


#: The prefix of a target keyed by the people a call reaches.
RECIPIENTS = "recipients#"
#: The prefix of a call whose declared recipients the guard cannot read.
UNPARSABLE = "unparsable-recipients#"

UNPARSABLE_REFUSAL = (
    "ERROR: the recipients of this message could not be read ({target}), so "
    "it is not sent without a person approving it. Name each recipient as a "
    "plain email address and try again."
)


def is_unparsable(target: str) -> bool:
    return target.startswith(UNPARSABLE)


def refusal_for(target: str) -> str:
    """What the model is told when the guard refuses a call to `target`."""
    if is_unparsable(target):
        return UNPARSABLE_REFUSAL.format(target=target[len(UNPARSABLE) :])
    return REFUSAL.format(target=target)


def _normalised_address(raw: str) -> str | None:
    """One mailbox, comparably -- the way the mail bridges compare them:
    trimmed and lowercased, the domain in its IDNA (ASCII) form without a
    trailing dot, plus-addressing kept (`ann+x@` is another mailbox). None
    for anything that is not an address."""
    address = raw.strip().lower()
    local, at, domain = address.rpartition("@")
    domain = domain.rstrip(".")
    if not at or not local or not domain or any(c.isspace() for c in address):
        return None
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if "." not in domain:
        return None
    return f"{local}@{domain}"


def _parsed_item(item: str) -> list[str] | None:
    """The addresses one recipient item names -- every one of them, or None
    when any part of it does not parse. Parsed on its own, so a bad item
    cannot change how its neighbours parse."""
    pairs = getaddresses([item])
    found: list[str] = []
    for _name, address in pairs:
        normalised = _normalised_address(address)
        if normalised is None:
            return None
        found.append(normalised)
    return found


def _declared_recipients(
    tool_name: str, arguments: dict[str, Any], spec: dict[str, Any]
) -> list[str] | None:
    """The people a call reaches, from the arguments its connection declares
    for that (`focus_spec.outward_fields`: tool -> argument names), as one
    set: each field and each list item parsed on its own as an address
    header (`"Doe, Ann" <ann@x.com>` is one person), normalised
    (`_normalised_address`), deduplicated and sorted, whichever field
    (to/cc/bcc) names them.

    Fails closed: None when any non-empty item does not parse fully into
    addresses -- a nested list, a non-string, a malformed address -- since
    the mailer may still deliver to what the guard could not read. Empty
    when nothing is declared or nobody is named."""
    declared = spec.get("outward_fields")
    fields = declared.get(tool_name) if isinstance(declared, dict) else None
    if not isinstance(fields, list):
        return []
    found: set[str] = set()
    for field in fields:
        value = arguments.get(str(field))
        items = value if isinstance(value, list) else [value]
        for item in items:
            if item is None or (isinstance(item, str) and not item.strip()):
                continue
            if not isinstance(item, str):
                return None
            parsed = _parsed_item(item)
            if parsed is None:
                return None
            found.update(parsed)
    return sorted(found)


def _declares_recipients(tool_name: str, spec: dict[str, Any]) -> bool:
    declared = spec.get("outward_fields")
    return isinstance(declared, dict) and isinstance(declared.get(tool_name), list)


def _people(target: str) -> list[str]:
    """The per-person targets a recipients target stands for: each person is
    remembered, and checked, on their own."""
    if not target.startswith(RECIPIENTS):
        return [target]
    return [f"recipient#{a}" for a in target[len(RECIPIENTS) :].split(",") if a]


def _marker(target: str) -> str:
    return f"outward:{target}"


async def already_delivered(
    db: AsyncSession, *, tenant_id: uuid.UUID, task_id: uuid.UUID, target: str
) -> bool:
    """Whether this target -- for a recipients target: ANY of its people --
    was reached in the task. `[a]` then `[a, b]` reaches a twice. An
    unreadable target always counts: nobody can tell whom it reaches."""
    if is_unparsable(target):
        return True
    for one in _people(target):
        if (
            await replayed_result(
                db, tenant_id=tenant_id, task_id=task_id, tool=_marker(one), arguments={}
            )
        ) is not None:
            return True
    return False


async def remember_delivery(
    db: AsyncSession, *, tenant_id: uuid.UUID, task_id: uuid.UUID, target: str
) -> None:
    """Record that a person has been reached. Only ever after a call SUCCEEDED --
    remembering a failed send would silence the retry that should happen."""
    for one in _people(target):
        await record_invocation(
            db,
            tenant_id=tenant_id,
            task_id=task_id,
            tool=_marker(one),
            arguments={},
            result=target,
        )


async def claim_delivery(
    db: AsyncSession, *, tenant_id: uuid.UUID, task_id: uuid.UUID, target: str
) -> bool:
    """Record this target as reached, BEFORE the call that reaches it: True
    only for the one request that inserted the marker (`claim_invocation`).
    A concurrent claim of the same target waits for this transaction and
    then loses, so of two racing requests exactly one goes on."""
    return await claim_invocation(
        db,
        tenant_id=tenant_id,
        task_id=task_id,
        tool=_marker(target),
        arguments={},
        result=target,
    )
