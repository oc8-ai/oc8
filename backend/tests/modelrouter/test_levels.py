"""The three reasoning levels and how a configured value reads as one
(spec 2026-11-11 §2 and §5)."""

from __future__ import annotations

import pytest

from oc8.modelrouter.levels import (
    DEFAULT_LEVEL,
    LEVELS,
    as_level,
    default_display,
    level_for,
    resolve_reasoning,
)


def test_there_are_exactly_three_levels_and_balanced_is_the_default() -> None:
    assert LEVELS == ("fast", "balanced", "thorough")
    assert DEFAULT_LEVEL == "balanced"


@pytest.mark.parametrize("raw", ["fast", "balanced", "thorough"])
def test_as_level_accepts_a_level_verbatim(raw: str) -> None:
    assert as_level(raw) == raw


@pytest.mark.parametrize("raw", ["Fast", "high", "", None, 3, "max"])
def test_as_level_rejects_anything_else(raw: object) -> None:
    assert as_level(raw) is None


@pytest.mark.parametrize(
    ("configured", "level"),
    [
        ("low", "fast"),
        ("medium", "balanced"),
        ("high", "thorough"),
        (" HIGH ", "thorough"),
        ("Medium", "balanced"),
        ("thorough", "thorough"),
        ("max", None),
        ("medium-high", None),
        (None, None),
    ],
)
def test_level_for_maps_the_legacy_values(configured: str | None, level: str | None) -> None:
    assert level_for(configured) == level


def test_default_display_names_the_level_custom_or_balanced() -> None:
    assert default_display(None) == "balanced"
    assert default_display("low") == "fast"
    assert default_display("max") == "custom"


def test_a_model_that_cannot_think_gets_neither_field() -> None:
    assert resolve_reasoning(supports=False, configured="high", chat_level="thorough") == (
        None,
        None,
    )


def test_without_a_model_config_the_legacy_effort_passes_untouched() -> None:
    assert resolve_reasoning(supports=None, configured="max", chat_level="fast") == (None, "max")


def test_the_chat_pick_wins_and_carries_no_free_form_effort() -> None:
    assert resolve_reasoning(supports=True, configured="high", chat_level="fast") == ("fast", None)


def test_a_legacy_value_maps_and_is_kept_as_effort() -> None:
    assert resolve_reasoning(supports=True, configured="HIGH", chat_level=None) == (
        "thorough",
        "HIGH",
    )


def test_a_custom_value_is_effort_only() -> None:
    assert resolve_reasoning(supports=True, configured="max", chat_level=None) == (None, "max")


def test_nothing_configured_on_a_thinking_model_is_balanced() -> None:
    assert resolve_reasoning(supports=True, configured=None, chat_level=None) == ("balanced", None)
