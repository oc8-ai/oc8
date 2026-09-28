"""A server that asks for a missing field, instead of the model inventing one.

The MCP call returns an elicitation rather than a result. The run parks in the
same inbox as ask_user. The person's answer is sent back as the arguments of
the same tool, and that call is what finishes.
"""

from __future__ import annotations

import json
from typing import Any


class ElicitationNeeded(Exception):
    """The server refused to finish until a person supplies the missing field."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _message_from_structured(structured: Any) -> str | None:
    if not isinstance(structured, dict):
        return None
    raw = structured.get("elicitation")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if isinstance(raw, dict):
        message = raw.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return None


def elicitation_message(result: Any) -> str | None:
    """The question on a tool result, or None when the result is ordinary.

    The SDK attribute is `structured_content`; the wire name is
    `structuredContent`. A 2026-07-28 server instead returns an
    `InputRequiredResult` whose form request carries the same question.
    """
    for attr in ("structured_content", "structuredContent"):
        message = _message_from_structured(getattr(result, attr, None))
        if message:
            return message
    requests = getattr(result, "input_requests", None)
    if isinstance(requests, dict):
        for req in requests.values():
            params = getattr(req, "params", None)
            message = getattr(params, "message", None) if params is not None else None
            if isinstance(message, str) and message.strip():
                return message.strip()
    return None


def arguments_with_answer(arguments: dict[str, Any], answer: str) -> dict[str, Any]:
    """The same call, with the person's answer filled in.

    A JSON object is merged onto the original arguments. Anything else is the
    single field `elicitation_answer`, which the model does not get to invent.
    """
    merged = dict(arguments)
    text = answer.strip()
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            merged.update(payload)
            return merged
    merged["elicitation_answer"] = text
    return merged
