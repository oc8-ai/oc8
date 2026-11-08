"""Structured `ask_user` (spec §4.1).

Backward compatible by construction: a call with only `question` is one
free-text question, and the readable text form (`render_questions_text`) is
what every runtime, the inbox and any model sees. The structure itself never
rides inside that text: it travels in its own field from the tool to the
executor (`ControlOutcome.questions`, `RunResult.pending_questions`) and is
parked on the run as `run.context["pending_questions"]`.

Messenger doors have no buttons, so `format_for_messenger` numbers questions
and options and `parse_messenger_answer` reads replies like `1,3` or
`1: 2; 2: 1,3` -- anything else is the free-text answer to the first question.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

MAX_QUESTIONS = 6
MAX_OPTIONS = 8
#: The longest written answer accepted, on every door (the chat request caps
#: it too; a messenger reply reaches `validate_answers` uncapped).
MAX_ANSWER_TEXT = 2000
#: The single free-text question a plain `question` call becomes.
PLAIN_ID = "q1"

Kind = Literal["single", "multi", "text"]
_KINDS: tuple[Kind, ...] = ("single", "multi", "text")

Answer = dict[str, list[str] | str]


class QuestionError(ValueError):
    """A malformed question or answer, phrased so a model or a person can fix it."""


@dataclass(frozen=True)
class QuestionOption:
    key: str
    label: str
    detail: str = ""


@dataclass(frozen=True)
class Question:
    id: str
    prompt: str
    kind: Kind
    options: tuple[QuestionOption, ...]
    allow_other: bool
    required: bool


def _question(raw: Any, index: int) -> Question:
    if not isinstance(raw, Mapping):
        raise QuestionError(f"question {index + 1} must be an object")
    qid = str(raw.get("id") or "").strip()
    prompt = str(raw.get("prompt") or "").strip()
    kind = str(raw.get("kind") or "text")
    if not qid or not prompt:
        raise QuestionError(f"question {index + 1} needs an id and a prompt")
    if kind not in _KINDS:
        raise QuestionError(f"question {qid!r}: kind must be single, multi or text")
    raw_options = raw.get("options") or []
    if not isinstance(raw_options, list):
        raise QuestionError(f"question {qid!r}: options must be a list")
    if len(raw_options) > MAX_OPTIONS:
        raise QuestionError(f"question {qid!r}: at most {MAX_OPTIONS} options")
    options: list[QuestionOption] = []
    seen: set[str] = set()
    for opt in raw_options:
        if not isinstance(opt, Mapping) or not opt.get("key") or not opt.get("label"):
            raise QuestionError(f"question {qid!r}: every option needs a key and a label")
        key = str(opt["key"])
        if key in seen:
            raise QuestionError(f"question {qid!r}: duplicate option key {key!r}")
        seen.add(key)
        options.append(
            QuestionOption(key=key, label=str(opt["label"]), detail=str(opt.get("detail") or ""))
        )
    if kind != "text" and not options:
        raise QuestionError(f"question {qid!r} needs options")
    return Question(
        id=qid,
        prompt=prompt,
        kind=kind,
        options=tuple(options),
        allow_other=bool(raw.get("allow_other", False)),
        required=bool(raw.get("required", True)),
    )


def parse_questions(arguments: Mapping[str, Any]) -> list[Question]:
    """`questions` if present (validated, limits enforced), else one free-text
    question from `question`. Raises `QuestionError` with a model-readable
    sentence."""
    raw = arguments.get("questions")
    if raw is None:
        text = str(arguments.get("question") or "").strip()
        if not text:
            raise QuestionError("ask_user requires a non-empty question")
        return [
            Question(
                id=PLAIN_ID, prompt=text, kind="text", options=(), allow_other=False, required=True
            )
        ]
    if not isinstance(raw, list) or not raw:
        raise QuestionError("questions must be a non-empty list")
    if len(raw) > MAX_QUESTIONS:
        raise QuestionError(f"ask at most {MAX_QUESTIONS} questions at once")
    questions = [_question(item, i) for i, item in enumerate(raw)]
    ids = [q.id for q in questions]
    if len(set(ids)) != len(ids):
        raise QuestionError("duplicate question id")
    return questions


def is_plain(questions: Sequence[Question]) -> bool:
    """One free-text question: nothing a plain text question does not already say."""
    return len(questions) == 1 and questions[0].kind == "text"


def questions_to_json(questions: Sequence[Question]) -> list[dict[str, Any]]:
    return [
        {
            "id": q.id,
            "prompt": q.prompt,
            "kind": q.kind,
            "options": [{"key": o.key, "label": o.label, "detail": o.detail} for o in q.options],
            "allow_other": q.allow_other,
            "required": q.required,
        }
        for q in questions
    ]


def questions_from_json(raw: Any) -> list[Question]:
    return parse_questions({"questions": raw})


def render_questions_text(questions: Sequence[Question]) -> str:
    """The readable form every model, the inbox and the transcript see."""
    if is_plain(questions):
        return questions[0].prompt
    lines: list[str] = []
    for q in questions:
        lines.append(q.prompt)
        lines.extend(f"- {o.label}" + (f" ({o.detail})" if o.detail else "") for o in q.options)
    return "\n".join(lines)


def validate_answers(questions: Sequence[Question], answers: Mapping[str, Any]) -> Answer:
    """`{question id: [option keys] | free text}`, checked against the questions."""
    unknown = set(answers) - {q.id for q in questions}
    if unknown:
        raise QuestionError(f"unknown question id {sorted(unknown)[0]!r}")
    out: Answer = {}
    for q in questions:
        value = answers.get(q.id)
        if isinstance(value, str):
            value = value.strip()
        if value is None or value == "" or value == []:
            if q.required:
                raise QuestionError(f"{q.prompt!r} is required")
            continue
        if isinstance(value, str):
            if len(value) > MAX_ANSWER_TEXT:
                raise QuestionError(f"{q.prompt!r}: at most {MAX_ANSWER_TEXT} characters")
            if q.kind != "text" and not q.allow_other:
                raise QuestionError(f"{q.prompt!r} takes one of its options")
            out[q.id] = value
            continue
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise QuestionError(f"{q.prompt!r}: answer with option keys or text")
        if q.kind == "text":
            raise QuestionError(f"{q.prompt!r} takes a written answer")
        keys = {o.key for o in q.options}
        if any(v not in keys for v in value):
            raise QuestionError(f"{q.prompt!r}: unknown option")
        if q.kind == "single" and len(value) != 1:
            raise QuestionError(f"{q.prompt!r} takes exactly one option")
        out[q.id] = list(dict.fromkeys(value))
    return out


def render_answers_text(
    questions: Sequence[Question], answers: Mapping[str, list[str] | str]
) -> str:
    """One readable line per answered question."""
    lines: list[str] = []
    for q in questions:
        value = answers.get(q.id)
        if value is None:
            continue
        if isinstance(value, str):
            lines.append(f"{q.prompt}: {value}")
        else:
            labels = {o.key: o.label for o in q.options}
            lines.append(f"{q.prompt}: {', '.join(labels.get(v, v) for v in value)}")
    return "\n".join(lines)


def format_for_messenger(questions: Sequence[Question]) -> str:
    """Numbered questions and options for a door without buttons."""
    lines: list[str] = []
    for i, q in enumerate(questions, start=1):
        lines.append(f"{i}. {q.prompt}" if len(questions) > 1 else q.prompt)
        lines.extend(f"  {j}) {o.label}" for j, o in enumerate(q.options, start=1))
    if any(q.options for q in questions):
        lines.append("")
        lines.append(
            "Reply with numbers, e.g. 1,3"
            if len(questions) == 1
            else "Reply like 1: 2; 2: 1,3 -- or just write your answer."
        )
    return "\n".join(lines)


def _picks(question: Question, text: str) -> list[str] | None:
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts or not all(p.isdigit() for p in parts):
        return None
    indices = [int(p) for p in parts]
    if any(i < 1 or i > len(question.options) for i in indices):
        return None
    return [question.options[i - 1].key for i in indices]


def parse_messenger_answer(questions: Sequence[Question], text: str) -> Answer:
    """`"1,3"` picks options of a single question; `"1: 2; 2: yes"` answers
    several; anything else is the free-text answer to the first question."""
    body = text.strip()
    if not questions:
        return {}
    first = questions[0]
    if len(questions) == 1:
        picks = _picks(first, body) if first.options else None
        return {first.id: picks if picks is not None else body}
    segments = [s.strip() for s in body.split(";") if s.strip()]
    out: Answer = {}
    for segment in segments:
        head, sep, tail = segment.partition(":")
        if not sep or not head.strip().isdigit():
            return {first.id: body}
        index = int(head.strip())
        if index < 1 or index > len(questions):
            return {first.id: body}
        question = questions[index - 1]
        picks = _picks(question, tail) if question.options else None
        out[question.id] = picks if picks is not None else tail.strip()
    return out or {first.id: body}
