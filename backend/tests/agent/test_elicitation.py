"""A server question parks; the answer completes the same call."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from oc8.agent.elicitation import ElicitationNeeded, arguments_with_answer, elicitation_message
from oc8.agent.mcp_client import McpSession


def test_elicitation_message_reads_the_sdk_attribute_and_a_form_request() -> None:
    assert (
        elicitation_message(SimpleNamespace(structured_content={"elicitation": "Which mailbox?"}))
        == "Which mailbox?"
    )
    params = SimpleNamespace(message="Which mailbox?")
    request = SimpleNamespace(params=params)
    assert (
        elicitation_message(SimpleNamespace(input_requests={"q": request})) == "Which mailbox?"
    )


def test_elicitation_message_reads_a_string_or_a_message_field() -> None:
    assert elicitation_message(SimpleNamespace(structuredContent={"elicitation": "Which mailbox?"})) == (
        "Which mailbox?"
    )
    assert (
        elicitation_message(
            SimpleNamespace(structuredContent={"elicitation": {"message": "Which mailbox?"}})
        )
        == "Which mailbox?"
    )
    assert elicitation_message(SimpleNamespace(structuredContent={"ok": True})) is None
    assert elicitation_message(SimpleNamespace()) is None


def test_a_json_answer_merges_and_prose_becomes_one_field() -> None:
    assert arguments_with_answer({"subject": "hi"}, '{"to": "a@b.c"}') == {
        "subject": "hi",
        "to": "a@b.c",
    }
    filled = arguments_with_answer({"subject": "hi"}, "use the sales mailbox")
    assert filled == {"subject": "hi", "elicitation_answer": "use the sales mailbox"}


def test_broken_json_is_not_merged_as_an_object() -> None:
    filled = arguments_with_answer({}, "{not json")
    assert filled == {"elicitation_answer": "{not json"}


@pytest.mark.asyncio
async def test_a_tool_call_raises_the_question_instead_of_returning_it() -> None:
    class _Server:
        async def call_tool(self, name: str, arguments: dict) -> SimpleNamespace:
            assert name == "mail_create_draft"
            return SimpleNamespace(
                structuredContent={"elicitation": "Which mailbox?"},
                content=[SimpleNamespace(text="not the answer")],
                is_error=False,
            )

    session = McpSession("python3", ["-c", "pass"])
    session._session = _Server()  # type: ignore[assignment]
    with pytest.raises(ElicitationNeeded) as caught:
        await session.call("mail_create_draft", {"subject": "hi"})
    assert caught.value.message == "Which mailbox?"
