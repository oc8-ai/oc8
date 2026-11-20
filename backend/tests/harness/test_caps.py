"""ModelCaps come from `model_config.params` (the dict that already carries
`supports_vision`), so a provider capa declares them in its manifest. Unknown
or malformed -> the conservative default; nothing branches on a model name."""

from __future__ import annotations

from oc8.agent.harness.caps import ModelCaps, resolve_caps


def test_defaults_are_the_conservative_ones_from_the_spec() -> None:
    caps = ModelCaps()
    assert caps.parallel_tool_calls is False
    assert caps.mid_conversation_system is True
    assert caps.tool_list_may_change is True
    assert caps.code_mode is False
    assert caps.context_window_tokens == 128_000
    assert caps.prompt_caching is False
    assert caps.compaction_summary_model is None


def test_none_and_empty_params_resolve_to_defaults() -> None:
    assert resolve_caps(None) == ModelCaps()
    assert resolve_caps({}) == ModelCaps()
    assert resolve_caps({"supports_vision": True}) == ModelCaps()


def test_declared_flags_are_honoured() -> None:
    caps = resolve_caps(
        {
            "parallel_tool_calls": True,
            "mid_conversation_system": False,
            "tool_list_may_change": False,
            "code_mode": True,
            "context_window_tokens": 200_000,
            "prompt_caching": True,
            "compaction_summary_model": "small-model",
        }
    )
    assert caps == ModelCaps(
        parallel_tool_calls=True,
        mid_conversation_system=False,
        tool_list_may_change=False,
        code_mode=True,
        context_window_tokens=200_000,
        prompt_caching=True,
        compaction_summary_model="small-model",
    )


def test_wrong_types_are_ignored_not_raised() -> None:
    caps = resolve_caps(
        {
            "parallel_tool_calls": "yes",
            "context_window_tokens": "big",
            "compaction_summary_model": "",
            "prompt_caching": 1,
        }
    )
    assert caps == ModelCaps()


def test_a_non_positive_window_is_ignored() -> None:
    assert resolve_caps({"context_window_tokens": 0}) == ModelCaps()
    assert resolve_caps({"context_window_tokens": True}) == ModelCaps()


def test_caps_are_immutable() -> None:
    caps = ModelCaps()
    try:
        caps.code_mode = True  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("ModelCaps must be frozen")
