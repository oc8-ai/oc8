import uuid

from oc8.modelrouter.adapters._openai_common import build_payload
from oc8.modelrouter.types import (
    CompletionRequest,
    ModelParams,
    NeutralMessage,
    with_prompt_cache_key,
)


def test_with_prompt_cache_key_stamps_extra_without_dropping_other_keys():
    params = ModelParams(extra={"seed": 1})
    stamped = with_prompt_cache_key(params, "run-1")
    assert stamped.extra == {"seed": 1, "prompt_cache_key": "run-1"}
    assert params.extra == {"seed": 1}
    assert with_prompt_cache_key(params, None) is params
    assert with_prompt_cache_key(params, "") is params


def test_openai_payload_forwards_prompt_cache_key():
    req = CompletionRequest(
        provider="openai",
        model="m",
        messages=[NeutralMessage(role="user", content="hi")],
        params=ModelParams(extra={"prompt_cache_key": "run-1"}),
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        agent_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        request_id=uuid.UUID("00000000-0000-0000-0000-000000000003"),
    )
    assert build_payload(req)["prompt_cache_key"] == "run-1"


def test_chat_run_uses_session_id_as_prompt_cache_key():
    from oc8.chat.replay import prompt_cache_key

    session, run = uuid.uuid4(), uuid.uuid4()
    chat = {"chat_session_id": str(session), "transcript_until": None}
    assert prompt_cache_key(chat, run) == str(session)
    assert prompt_cache_key({**chat, "prompt_cache_key": "pinned"}, run) == "pinned"


def test_other_runs_keep_run_id():
    from oc8.chat.replay import prompt_cache_key

    run = uuid.uuid4()
    assert prompt_cache_key({}, run) == str(run)
    assert prompt_cache_key(None, run) == str(run)
    assert prompt_cache_key({"chat_session_id": str(uuid.uuid4())}, run) == str(run)
    assert prompt_cache_key({}, None) is None


def test_a_malformed_chat_context_keeps_the_run_id():
    from oc8.chat.replay import prompt_cache_key

    run = uuid.uuid4()
    assert prompt_cache_key({"chat_session_id": "not-a-uuid", "transcript_until": None}, run) == (
        str(run)
    )
    bad_until = {"chat_session_id": str(uuid.uuid4()), "transcript_until": "nope"}
    assert prompt_cache_key(bad_until, run) == str(run)


def test_anthropic_payload_never_carries_the_openai_cache_key():
    """Every run stamps `prompt_cache_key` (an OpenAI field) into `extra`, and
    Anthropic refuses unknown top-level fields with a 400 ("Extra inputs are
    not permitted"), so no Claude model answered at all. Claude caches through
    `cache_control` instead."""
    from oc8.modelrouter.adapters.anthropic import _build_payload

    req = CompletionRequest(
        provider="anthropic",
        model="claude-sonnet-5-5",
        messages=[NeutralMessage(role="user", content="hi")],
        params=with_prompt_cache_key(ModelParams(extra={"top_k": 5}), "run-1"),
    )
    for caching in (False, True):
        payload = _build_payload(req, prompt_caching=caching)
        assert "prompt_cache_key" not in payload
        assert payload["top_k"] == 5
