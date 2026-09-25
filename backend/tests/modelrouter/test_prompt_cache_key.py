from oc8.modelrouter.types import ModelParams, with_prompt_cache_key


def test_with_prompt_cache_key_stamps_extra_without_dropping_other_keys():
    params = ModelParams(extra={"seed": 1})
    stamped = with_prompt_cache_key(params, "run-1")
    assert stamped.extra == {"seed": 1, "prompt_cache_key": "run-1"}
    assert params.extra == {"seed": 1}
    assert with_prompt_cache_key(params, None) is params
    assert with_prompt_cache_key(params, "") is params
