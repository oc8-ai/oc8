import uuid

from oc8.modelrouter.adapters._openai_common import build_payload
from oc8.modelrouter.types import CompletionRequest, ModelParams, NeutralMessage, with_prompt_cache_key


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
