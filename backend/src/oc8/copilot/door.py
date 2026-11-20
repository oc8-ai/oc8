"""Which door a turn came through (design §7a.6). A messenger's reason for not
offering ask_user still holds; web and follow-up turns can be answered from
"Waiting on me"."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

Door = Literal["web", "followup", "telegram"]


def door_of(context: Mapping[str, Any] | None) -> Door:
    """`chat.service.send_message` stamps context["door"]; the fallbacks cover
    runs created before that existed (a channel without a follow-up marker is a
    messenger turn)."""
    ctx = context or {}
    if "door" in ctx:
        stamped = ctx["door"]
        if stamped in ("web", "followup", "telegram"):
            return stamped  # type: ignore[no-any-return]
        # Present but unrecognised: fail toward the most restrictive door
        # (ask_user withheld) rather than open to web.
        return "telegram"
    if isinstance(ctx.get("followup"), dict):
        return "followup"
    if ctx.get("chat_channel") and ctx.get("chat_channel_external_id"):
        return "telegram"
    return "web"


def chat_door(source: str | None, context: Mapping[str, Any] | None) -> Door | None:
    """The door a CHAT run was stamped with, or None.

    For a gate that must fail closed (the edition offer hides a
    door-restricted tool when the door is None). Only the stamp counts --
    none of `door_of`'s fallbacks, which guess "web" for a run with nothing
    and "telegram" for one that merely carries a `chat_channel`. A run that is
    not `source="chat"` has no door even when it carries a stamp: a delegated
    sub-run inherits its parent's `door` and channel (`_delegate`) but is
    nobody's turn. An unrecognised stamp is None too.
    """
    if source != "chat" or not context:
        return None
    stamped = context.get("door")
    if stamped in ("web", "followup", "telegram"):
        return stamped  # type: ignore[no-any-return]
    return None
