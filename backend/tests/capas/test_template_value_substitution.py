"""Unit tests for the generic {{field_key}} template-value substitution
mechanism (design doc: 2026-09-20-engineering-dev-agent-capa-design.md §1).
Integration coverage (instantiate_department wiring, real setup config,
the "no setup ever run" regression guard) lives in this same file, below
the pure-function tests -- see that section's own docstrings."""

from __future__ import annotations

from oc8.capas.service import _substitute_template_values


def test_replaces_a_present_token() -> None:
    assert (
        _substitute_template_values("Check {{repo}}.", {"repo": "acme/widgets"})
        == "Check acme/widgets."
    )


def test_leaves_a_missing_token_literally_in_place() -> None:
    assert _substitute_template_values("Check {{repo}}.", {}) == "Check {{repo}}."


def test_text_with_no_tokens_is_unchanged() -> None:
    assert (
        _substitute_template_values("Plain mission text.", {"repo": "acme/widgets"})
        == "Plain mission text."
    )


def test_replaces_multiple_distinct_tokens() -> None:
    text = "Work {{repo}} issues labeled {{label}}."
    config = {"repo": "acme/widgets", "label": "agent-ready"}
    assert (
        _substitute_template_values(text, config)
        == "Work acme/widgets issues labeled agent-ready."
    )


def test_replaces_a_repeated_token_every_time_it_appears() -> None:
    text = "{{repo}} -- clone {{repo}} and work in it."
    config = {"repo": "acme/widgets"}
    assert (
        _substitute_template_values(text, config)
        == "acme/widgets -- clone acme/widgets and work in it."
    )
