from __future__ import annotations

import pytest

from oc8.chat.questions import (
    MAX_OPTIONS,
    MAX_QUESTIONS,
    QuestionError,
    format_for_messenger,
    parse_messenger_answer,
    parse_questions,
    questions_from_json,
    questions_to_json,
    render_answers_text,
    render_questions_text,
    validate_answers,
)

TWO = {
    "questions": [
        {
            "id": "region",
            "prompt": "Which region?",
            "kind": "single",
            "options": [{"key": "eu", "label": "EU"}, {"key": "us", "label": "US"}],
            "allow_other": True,
            "required": True,
        },
        {
            "id": "channels",
            "prompt": "Which channels?",
            "kind": "multi",
            "options": [
                {"key": "mail", "label": "Mail"},
                {"key": "sms", "label": "SMS"},
                {"key": "push", "label": "Push"},
            ],
            "allow_other": False,
            "required": False,
        },
    ]
}


def test_limits() -> None:
    assert MAX_QUESTIONS == 6 and MAX_OPTIONS == 8


def test_plain_question_stays_one_free_text_question() -> None:
    [q] = parse_questions({"question": "What is the order number?"})
    assert q.kind == "text" and q.prompt == "What is the order number?" and q.options == ()
    assert render_questions_text([q]) == "What is the order number?"


def test_empty_call_is_refused() -> None:
    with pytest.raises(QuestionError, match="non-empty"):
        parse_questions({"question": "  "})


def test_limits_are_enforced() -> None:
    too_many = {"questions": [{"id": f"q{i}", "prompt": "x", "kind": "text"} for i in range(7)]}
    with pytest.raises(QuestionError, match="at most 6"):
        parse_questions(too_many)
    too_wide = {
        "questions": [
            {
                "id": "q",
                "prompt": "x",
                "kind": "single",
                "options": [{"key": str(i), "label": str(i)} for i in range(9)],
            }
        ]
    }
    with pytest.raises(QuestionError, match="at most 8"):
        parse_questions(too_wide)
    with pytest.raises(QuestionError, match="needs options"):
        parse_questions(
            {"questions": [{"id": "q", "prompt": "x", "kind": "single", "options": []}]}
        )
    with pytest.raises(QuestionError, match="duplicate"):
        parse_questions(
            {
                "questions": [
                    {"id": "a", "prompt": "x", "kind": "text"},
                    {"id": "a", "prompt": "y", "kind": "text"},
                ]
            }
        )


def test_json_round_trips() -> None:
    questions = parse_questions(TWO)
    assert questions_from_json(questions_to_json(questions)) == questions


def test_readable_text_lists_the_options() -> None:
    text = render_questions_text(parse_questions(TWO))
    assert text.splitlines()[:3] == ["Which region?", "- EU", "- US"]


def test_answers_validate_and_read_back() -> None:
    questions = parse_questions(TWO)
    answers = validate_answers(questions, {"region": ["eu"], "channels": ["mail", "push"]})
    assert (
        render_answers_text(questions, answers) == "Which region?: EU\nWhich channels?: Mail, Push"
    )
    other = validate_answers(questions, {"region": "Switzerland"})
    assert other == {"region": "Switzerland"}
    with pytest.raises(QuestionError, match="required"):
        validate_answers(questions, {"channels": ["sms"]})
    with pytest.raises(QuestionError, match="unknown option"):
        validate_answers(questions, {"region": ["mars"]})
    with pytest.raises(QuestionError, match="exactly one"):
        validate_answers(questions, {"region": ["eu", "us"]})
    with pytest.raises(QuestionError, match="one of its options"):
        validate_answers(questions, {"region": ["eu"], "channels": "carrier pigeon"})


def test_messenger_numbering_and_parsing() -> None:
    questions = parse_questions(TWO)
    text = format_for_messenger(questions)
    assert "1. Which region?" in text and "  1) EU" in text and "2. Which channels?" in text
    assert parse_messenger_answer(questions, "1: 1; 2: 1,3") == {
        "region": ["eu"],
        "channels": ["mail", "push"],
    }
    [single] = parse_questions({"questions": [TWO["questions"][1]]})
    assert parse_messenger_answer([single], "1,3") == {"channels": ["mail", "push"]}
    assert parse_messenger_answer(questions, "Switzerland, please") == {
        "region": "Switzerland, please"
    }


def test_each_answer_is_capped_on_every_door() -> None:
    """Enforced here, not only in the chat request schema: the inbox, the admin
    door and a messenger reply all end up in `validate_answers`."""
    from oc8.chat.questions import MAX_ANSWER_TEXT

    questions = parse_questions(TWO)
    with pytest.raises(QuestionError, match="at most 2000 characters"):
        validate_answers(questions, {"region": "x" * (MAX_ANSWER_TEXT + 1)})
    with pytest.raises(QuestionError, match="at most 8 options"):
        validate_answers(questions, {"region": ["eu"], "channels": ["mail"] * 9})
    with pytest.raises(QuestionError, match="unknown question id"):
        validate_answers(questions, {"region": ["eu"], "nope": "x"})
    # A messenger reply is free text too, and hits the same cap.
    long_reply = parse_messenger_answer(questions, "y" * (MAX_ANSWER_TEXT + 1))
    with pytest.raises(QuestionError, match="at most 2000 characters"):
        validate_answers(questions, long_reply)
