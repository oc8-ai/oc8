"""price_as_of() / cost_micros_from_price() -- versioned, DB-backed pricing.
Supersedes the old static-table cost_micros(provider, model, tokens_in,
tokens_out); the substring-matching cases below are preserved verbatim from
that version, now expressed against explicit PriceRow fixtures."""

from __future__ import annotations

import datetime as dt

from oc8.metering.pricing import PriceRow, cost_micros_from_price, price_as_of

_NOW = dt.datetime(2026, 8, 19, tzinfo=dt.UTC)
_YESTERDAY = _NOW - dt.timedelta(days=1)
_LAST_WEEK = _NOW - dt.timedelta(days=7)


def _rows() -> list[PriceRow]:
    return [
        PriceRow("anthropic", "claude35sonnet", 3.0, 15.0, _LAST_WEEK, True),
        PriceRow("anthropic", "claude35haiku", 0.8, 4.0, _LAST_WEEK, True),
        PriceRow("openai", "gpt4omini", 0.15, 0.6, _LAST_WEEK, True),
        PriceRow("openai", "gpt4o", 2.5, 10.0, _LAST_WEEK, True),
        PriceRow("openai_compatible", "mistrallarge", 2.0, 6.0, _LAST_WEEK, True),
    ]


def test_known_cloud_model_costs_tokens_times_rate() -> None:
    price = price_as_of(_rows(), "openai", "gpt-4o", _NOW)
    assert price == (2.5, 10.0)
    assert cost_micros_from_price(price, 1000, 500) == round(1000 * 2.5 + 500 * 10.0)


def test_gpt4o_mini_matched_before_gpt4o() -> None:
    price = price_as_of(_rows(), "openai", "gpt-4o-mini", _NOW)
    assert price == (0.15, 0.6)


def test_seeded_display_style_tag_matches() -> None:
    price = price_as_of(_rows(), "anthropic", "Claude 3.5 Sonnet", _NOW)
    assert price == (3.0, 15.0)


def test_real_api_id_form_matches() -> None:
    price = price_as_of(_rows(), "anthropic", "claude-3-5-sonnet-20241022", _NOW)
    assert price == (3.0, 15.0)


def test_unknown_model_returns_none() -> None:
    assert price_as_of(_rows(), "openai", "some-unknown-model", _NOW) is None


def test_mistral_large_matches() -> None:
    price = price_as_of(_rows(), "openai_compatible", "mistral-large-2", _NOW)
    assert price == (2.0, 6.0)


def test_a_price_change_does_not_affect_a_report_for_before_the_change() -> None:
    rows = [
        # Claude 4.x ids put the name before the version (e.g.
        # "claude-sonnet-4-5-..."), unlike 3.5's "claude-3-5-sonnet-..." --
        # pattern order must match.
        PriceRow("anthropic", "claudesonnet45", 3.0, 15.0, _LAST_WEEK, True),
        PriceRow("anthropic", "claudesonnet45", 5.0, 20.0, _YESTERDAY, True),
    ]
    # A usage record from before the price changed still sees the old price.
    old = price_as_of(
        rows, "anthropic", "claude-sonnet-4-5-20250929", _LAST_WEEK + dt.timedelta(hours=1)
    )
    assert old == (3.0, 15.0)
    # A usage record from after the change sees the new price.
    new = price_as_of(rows, "anthropic", "claude-sonnet-4-5-20250929", _NOW)
    assert new == (5.0, 20.0)


def test_deactivated_price_is_not_matched() -> None:
    rows = [PriceRow("anthropic", "claude45sonnet", 3.0, 15.0, _LAST_WEEK, False)]
    assert price_as_of(rows, "anthropic", "claude-sonnet-4-5-20250929", _NOW) is None


def test_a_pattern_saved_as_the_model_id_matches_that_model() -> None:
    """The Models page saves the model id as typed (`claude-haiku-4-5-20251001`).
    It is compared in the same normalised form as the model name, so a price
    entered that way applies instead of leaving the model at zero."""
    rows = [PriceRow("anthropic", "claude-haiku-4-5", 1.0, 5.0, _LAST_WEEK, True)]
    assert price_as_of(rows, "anthropic", "claude-haiku-4-5-20251001", _NOW) == (1.0, 5.0)


def test_a_pattern_with_capitals_and_spaces_matches() -> None:
    rows = [PriceRow("openai", "GPT 4.1", 2.0, 8.0, _LAST_WEEK, True)]
    assert price_as_of(rows, "openai", "gpt-4.1", _NOW) == (2.0, 8.0)


def test_the_longest_pattern_still_wins_after_normalising() -> None:
    rows = [
        PriceRow("openai", "gpt-4o", 2.5, 10.0, _LAST_WEEK, True),
        PriceRow("openai", "gpt-4o-mini", 0.15, 0.6, _LAST_WEEK, True),
    ]
    assert price_as_of(rows, "openai", "gpt-4o-mini-2024-07-18", _NOW) == (0.15, 0.6)
