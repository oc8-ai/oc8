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
