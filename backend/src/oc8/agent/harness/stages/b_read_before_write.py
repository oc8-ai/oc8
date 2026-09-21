"""B2 -- require an entity read before a write in the same run."""

from __future__ import annotations

from typing import Any

from oc8.agent.harness.state import EntityRef, Ledger


def entity_key(connection: str, kind: str, id: str) -> str:
    return f"{connection}/{kind}/{id}"


def note_access(
    ledger: Ledger,
    *,
    connection: str,
    kind: str,
    id: str,
    label: str,
    step_no: int,
    wrote: bool,
    tool: str,
) -> None:
    key = entity_key(connection, kind, id)
    entity = ledger.entities.setdefault(
        key,
        EntityRef(connection=connection, kind=kind, id=id, label=label),
    )
    if label:
        entity.label = label

    if wrote:
        entity.last_write_step = step_no
        if tool not in entity.write_tools:
            entity.write_tools.append(tool)
        return

    if entity.first_read_step is None:
        entity.first_read_step = step_no
    entity.last_read_step = step_no


def read_before_write_denial(
    *,
    tool: str,
    tier: str,
    identity: tuple[str, str] | None,
    ledger: Ledger,
    connection: str,
    config: dict[str, Any] | None,
    label: str,
) -> str | None:
    """Return the deny text, or None to allow."""
    del tool
    cfg = config or {}
    if cfg.get("read_before_write", True) is False or identity is None or tier == "read":
        return None

    kind, id = identity
    entity = ledger.entities.get(entity_key(connection, kind, id))
    if entity is not None and entity.last_read_step is not None:
        return None

    lookup_tools = cfg.get("entity_lookup_tools") or []
    suggestion = lookup_tools[0] if lookup_tools else "a search/read tool"
    return (
        f"Precondition not met: you have not read {label} ({kind} {id}) in this run.\n"
        f"Read it first — e.g. {suggestion} —\n"
        "then make the change. This is a policy decision, not a tool error."
    )
