"""The work digest a reply records and the restricted flag beside it."""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest

from oc8.chat import service as chat_service
from oc8.chat.digest import build_work_digest
from tests.chat.test_message_tree import _reply, _send, _session
from tests.conftest import AppSessionFactory

RUN = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _call(tool: str, arguments: dict[str, Any], state: str, result: str) -> dict[str, Any]:
    return {
        "tool": tool,
        "arguments": arguments,
        "result": result,
        "step": 1,
        "connection": "c",
        "state": state,
    }


def test_one_line_per_call_in_order_with_outcome() -> None:
    calls = [
        _call("crm_search", {"q": "acme"}, "done", "3 hits"),
        _call("send_mail", {"to": "x"}, "denied", ""),
    ]
    digest = build_work_digest(calls, run_id=RUN, restricted=False)
    assert digest is not None
    lines = digest.splitlines()
    assert lines[0].startswith("crm_search ") and " → ok: 3 hits" in lines[0]
    assert lines[1].startswith("send_mail ") and lines[1].endswith("→ denied")


def test_states_map_to_ok_denied_error_waiting() -> None:
    states = {"done": "ok", "denied": "denied", "failed": "error", "awaiting_approval": "waiting"}
    for state, outcome in states.items():
        digest = build_work_digest([_call("t", {}, state, "")], run_id=RUN, restricted=False)
        assert digest is not None and digest.endswith(f"→ {outcome}")


def test_arguments_and_preview_are_capped() -> None:
    digest = build_work_digest(
        [_call("t", {"a": "x" * 999}, "done", "y" * 999)], run_id=RUN, restricted=False
    )
    assert digest is not None
    match = re.fullmatch(r"t (.*) → ok: (.*)", digest)
    assert match is not None
    assert len(match.group(1)) <= 200 and len(match.group(2)) <= 300


def test_a_spilled_result_names_the_file_and_run() -> None:
    result = (
        '[crm_search returned 90000 chars (812 lines); kept as file "step-3-crm_search.txt". ...'
    )
    digest = build_work_digest(
        [_call("crm_search", {}, "done", result)], run_id=RUN, restricted=False
    )
    assert digest is not None
    assert 'file "step-3-crm_search.txt"' in digest
    assert f'read_run_file(filename="step-3-crm_search.txt", run_id="{RUN}")' in digest


def test_whole_digest_is_capped_with_a_more_marker() -> None:
    calls = [_call(f"t{i}", {}, "done", "z" * 300) for i in range(40)]
    digest = build_work_digest(calls, run_id=RUN, restricted=False)
    assert digest is not None
    assert len(digest) <= 1500 and re.search(r"… \(\+\d+ more calls\)$", digest)


def test_a_restricted_run_keeps_names_and_outcomes_only() -> None:
    digest = build_work_digest(
        [_call("crm_search", {"q": "secret"}, "done", "secret row")], run_id=RUN, restricted=True
    )
    assert digest is not None
    assert "crm_search" in digest and "secret" not in digest


def test_no_calls_no_digest() -> None:
    assert build_work_digest([], run_id=RUN, restricted=False) is None


@pytest.mark.asyncio
async def test_record_assistant_reply_stores_digest_and_restricted(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        _msg, run = await _send(db, session, "question")
        run.context = {
            **run.context,
            "toolCalls": [_call("crm_search", {"q": "acme"}, "done", "3 hits")],
            "contains_restricted": True,
        }
        reply = await _reply(db, run, "answer")
        assert reply.restricted is True
        assert reply.work_digest is not None
        assert reply.work_digest.startswith("crm_search")
        assert "acme" not in reply.work_digest


@pytest.mark.asyncio
async def test_a_failed_digest_still_records_the_reply(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> str:
        raise RuntimeError("digest broke")

    monkeypatch.setattr(chat_service, "build_work_digest", boom)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        _msg, run = await _send(db, session, "question")
        run.context = {**run.context, "toolCalls": [_call("t", {}, "done", "r")]}
        reply = await _reply(db, run, "answer")
        assert reply.content == "answer"
        assert reply.work_digest is None
        assert reply.restricted is False
