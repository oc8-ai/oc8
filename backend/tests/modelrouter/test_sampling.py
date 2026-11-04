"""Where a run's sampling parameters come from.

Both runtimes used to hardcode ModelParams(temperature=0.0, max_tokens=512) and
ModelConfig.params was read only for base_url. So an operator could not make an
agent less repetitive or give it room for a longer tool call without a code
change -- temperature 0.0 means the same task produces byte-identical output
forever, which is exactly what it looked like in the Odoo demo.
"""

from __future__ import annotations

import uuid
from typing import Any

from oc8 import models as m
from oc8.modelrouter.sampling import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    bumped_for_length_retry,
    resolve_params,
)
from oc8.modelrouter.types import ModelParams


def _agent(definition: dict[str, Any] | None = None) -> m.Agent:
    return m.Agent(
        id=uuid.uuid4(), tenant_id=uuid.uuid4(), department_id=uuid.uuid4(),
        name="Nora", status="idle", definition=definition or {}, presentation={},
    )


def _config(params: dict[str, Any] | None) -> m.ModelConfig:
    return m.ModelConfig(
        id=uuid.uuid4(), tenant_id=uuid.uuid4(), provider="opaas_ai",
        model="opaas_ai:odoo-gpt", params=params if params is not None else {},
    )


def test_without_any_configuration_the_framework_default_applies() -> None:
    p = resolve_params(None, agent=_agent())
    assert p.temperature == DEFAULT_TEMPERATURE
    assert p.max_tokens == DEFAULT_MAX_TOKENS


def test_the_model_config_can_set_both() -> None:
    p = resolve_params(_config({"temperature": 0.6, "max_tokens": 2048}), agent=_agent())
    assert p.temperature == 0.6
    assert p.max_tokens == 2048


def test_base_url_in_params_is_not_mistaken_for_a_sampling_knob() -> None:
    """params also carries transport config; only the sampling keys are read."""
    p = resolve_params(_config({"base_url": "http://x:1234"}), agent=_agent())
    assert p.temperature == DEFAULT_TEMPERATURE
    assert p.max_tokens == DEFAULT_MAX_TOKENS


def test_the_agent_overrides_its_model_config() -> None:
    """One model config is shared by many agents; a single agent must be tunable
    without changing sampling for everyone else on that model."""
    p = resolve_params(
        _config({"temperature": 0.1, "max_tokens": 512}),
        agent=_agent({"model_params": {"temperature": 0.8, "max_tokens": 4096}}),
    )
    assert p.temperature == 0.8
    assert p.max_tokens == 4096


def test_a_partial_override_keeps_the_other_value() -> None:
    p = resolve_params(
        _config({"temperature": 0.3, "max_tokens": 2048}),
        agent=_agent({"model_params": {"temperature": 0.9}}),
    )
    assert p.temperature == 0.9
    assert p.max_tokens == 2048, "an unset key must not fall back past the model config"


def test_a_nonsense_value_falls_back_instead_of_failing_the_run() -> None:
    """params is free-form JSONB an operator edits. A typo there must not take a
    run down -- it degrades to the default."""
    p = resolve_params(_config({"temperature": "warm", "max_tokens": None}), agent=_agent())
    assert p.temperature == DEFAULT_TEMPERATURE
    assert p.max_tokens == DEFAULT_MAX_TOKENS


def test_out_of_range_values_are_clamped_not_passed_through() -> None:
    """Providers reject temperature > 2 (or negative) with a hard error, which
    would surface as a failed run rather than a misconfiguration."""
    hot = resolve_params(_config({"temperature": 9.5}), agent=_agent())
    cold = resolve_params(_config({"temperature": -1.0}), agent=_agent())
    assert hot.temperature == 2.0
    assert cold.temperature == 0.0


def test_a_non_positive_token_budget_falls_back() -> None:
    for bad in (0, -100):
        p = resolve_params(_config({"max_tokens": bad}), agent=_agent())
        assert p.max_tokens == DEFAULT_MAX_TOKENS


def test_an_agent_without_a_definition_is_fine() -> None:
    agent = _agent()
    agent.definition = {}
    p = resolve_params(_config({"temperature": 0.4}), agent=agent)
    assert p.temperature == 0.4


def test_no_agent_at_all_still_resolves() -> None:
    """The coding loop and other callers may not have an Agent to hand."""
    p = resolve_params(_config({"temperature": 0.5}), agent=None)
    assert p.temperature == 0.5


def test_effort_is_unset_by_default() -> None:
    p = resolve_params(None, agent=_agent())
    assert p.effort is None


def test_the_model_config_can_set_effort() -> None:
    p = resolve_params(_config({"effort": "high"}), agent=_agent())
    assert p.effort == "high"


def test_effort_is_forwarded_as_is_not_validated_against_a_fixed_set() -> None:
    """The provider owns what it accepts; oc8 does not guess at today's set."""
    p = resolve_params(_config({"effort": "medium-plus"}), agent=_agent())
    assert p.effort == "medium-plus"


def test_the_agent_overrides_the_model_configs_effort() -> None:
    p = resolve_params(
        _config({"effort": "low"}),
        agent=_agent({"model_params": {"effort": "high"}}),
    )
    assert p.effort == "high"


def test_a_blank_effort_falls_back_to_unset() -> None:
    p = resolve_params(_config({"effort": "   "}), agent=_agent())
    assert p.effort is None


def test_a_non_string_effort_falls_back_to_unset() -> None:
    p = resolve_params(_config({"effort": 3}), agent=_agent())
    assert p.effort is None


def test_a_length_retry_carries_effort_through() -> None:
    """A doubled max_tokens budget must not silently drop the effort setting."""
    bumped = bumped_for_length_retry(ModelParams(temperature=0.5, max_tokens=100, effort="high"))
    assert bumped.effort == "high"
    assert bumped.max_tokens == 200


def test_extra_is_unset_by_default() -> None:
    p = resolve_params(None, agent=_agent())
    assert p.extra is None


def test_the_model_config_can_set_extra() -> None:
    p = resolve_params(_config({"extra": {"top_p": 0.9}}), agent=_agent())
    assert p.extra == {"top_p": 0.9}


def test_the_agent_merges_extra_keys_into_the_model_configs_extra() -> None:
    """Unlike temperature/max_tokens/effort, extra merges key-by-key: an agent
    overriding one raw key must not drop the model's own raw keys it didn't
    mention."""
    p = resolve_params(
        _config({"extra": {"top_p": 0.9, "provider": {"order": ["a"]}}}),
        agent=_agent({"model_params": {"extra": {"top_p": 0.5}}}),
    )
    assert p.extra == {"top_p": 0.5, "provider": {"order": ["a"]}}


def test_a_non_dict_extra_falls_back_to_unset() -> None:
    p = resolve_params(_config({"extra": "not a dict"}), agent=_agent())
    assert p.extra is None


def test_an_empty_extra_dict_leaves_nothing_set() -> None:
    p = resolve_params(_config({"extra": {}}), agent=_agent())
    assert p.extra is None


def test_a_length_retry_carries_extra_through() -> None:
    bumped = bumped_for_length_retry(
        ModelParams(temperature=0.5, max_tokens=100, extra={"top_p": 0.9})
    )
    assert bumped.extra == {"top_p": 0.9}


# --- answer budget: OC8_DEFAULT_MAX_TOKENS and per-provider caps ---------------


def _use(monkeypatch: Any, **kw: Any) -> None:
    import oc8.modelrouter.sampling as sampling
    from oc8.config import Settings

    settings = Settings(**kw)
    monkeypatch.setattr(sampling, "get_settings", lambda: settings)


def test_the_default_budget_comes_from_the_setting(monkeypatch: Any) -> None:
    _use(monkeypatch, default_max_tokens=16384)
    assert resolve_params(None, agent=_agent()).max_tokens == 16384


def test_the_code_default_is_4096() -> None:
    from oc8.config import Settings

    assert Settings.model_fields["default_max_tokens"].default == 4096
    assert DEFAULT_MAX_TOKENS == 4096


def test_an_unusable_default_setting_falls_back_to_the_code_default(monkeypatch: Any) -> None:
    _use(monkeypatch, default_max_tokens=0)
    assert resolve_params(None, agent=_agent()).max_tokens == DEFAULT_MAX_TOKENS


def test_an_agent_budget_still_beats_the_setting(monkeypatch: Any) -> None:
    _use(monkeypatch, default_max_tokens=16384)
    p = resolve_params(None, agent=_agent({"model_params": {"max_tokens": 2000}}))
    assert p.max_tokens == 2000


def test_a_provider_cap_cuts_the_budget_down_instead_of_failing(monkeypatch: Any) -> None:
    _use(monkeypatch, default_max_tokens=16384, provider_max_tokens="opaas_ai=4096, other=100")
    assert resolve_params(_config({}), agent=_agent()).max_tokens == 4096


def test_a_provider_cap_does_not_touch_other_providers(monkeypatch: Any) -> None:
    _use(monkeypatch, default_max_tokens=16384, provider_max_tokens="ollama=4096")
    assert resolve_params(_config({}), agent=_agent()).max_tokens == 16384


def test_malformed_cap_pairs_are_skipped(monkeypatch: Any) -> None:
    _use(monkeypatch, provider_max_tokens="opaas_ai=lots,=5,ollama=-1,litellm=4096")
    from oc8.modelrouter.sampling import provider_max_tokens

    assert provider_max_tokens("opaas_ai") is None
    assert provider_max_tokens("ollama") is None
    assert provider_max_tokens("litellm") == 4096


def test_the_length_retry_never_goes_above_the_provider_cap(monkeypatch: Any) -> None:
    _use(monkeypatch, provider_max_tokens="opaas_ai=6000")
    bumped = bumped_for_length_retry(ModelParams(max_tokens=4096), provider="opaas_ai")
    assert bumped.max_tokens == 6000
    at_cap = bumped_for_length_retry(ModelParams(max_tokens=6000), provider="opaas_ai")
    assert at_cap.max_tokens == 6000  # same budget: the caller skips the retry


def test_the_budget_message_says_what_to_do() -> None:
    from oc8.modelrouter.sampling import budget_exhausted_message

    message = budget_exhausted_message(8192)
    assert "8,192 tokens" in message
    assert "max_tokens" in message
    assert "OC8_DEFAULT_MAX_TOKENS" in message
    assert "reasoning effort" in message
