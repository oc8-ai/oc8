"""Structural diff of two version payloads, one level deep inside a JSONB field.

One level, not a generic deep diff, and not a whole-object comparison. A
whole-object comparison renders "narrowing changed" for a single threshold
edit, which tells the operator nothing they did not already know. A generic
deep diff produces paths nobody can review and would have to invent a
rendering convention for arrays. `narrowing` is keyed by connection NAME and
`definition` is a flat grab-bag, so one level is exactly enough for both --
spec §2.7 says so in as many words.
"""

from __future__ import annotations

from typing import Any

from oc8.agents.versioning import changed_fields, diff_payloads


def test_a_scalar_change_is_one_entry() -> None:
    entries = diff_payloads({"mission": "a"}, {"mission": "b"})
    assert entries == [{"field": "mission", "before": "a", "after": "b"}]


def test_an_unchanged_field_produces_nothing() -> None:
    assert (
        diff_payloads(
            {"mission": "a", "role_title": "x"},
            {"mission": "a", "role_title": "x"},
        )
        == []
    )


def test_a_jsonb_field_diffs_one_level_deep() -> None:
    """The case this function exists for: one connection's threshold moved and
    the other connection's did not, so the diff names `narrowing.odoo` and
    says nothing about `narrowing.github`."""
    before = {"narrowing": {"odoo": {"modify": True}, "github": {"read": True}}}
    after = {"narrowing": {"odoo": {"modify": False}, "github": {"read": True}}}
    assert diff_payloads(before, after) == [
        {
            "field": "narrowing.odoo",
            "before": {"modify": True},
            "after": {"modify": False},
        }
    ]


def test_a_key_added_to_a_jsonb_field_reads_as_before_null() -> None:
    before: dict[str, Any] = {"narrowing": {}}
    after = {"narrowing": {"odoo": {"read": True}}}
    assert diff_payloads(before, after) == [
        {"field": "narrowing.odoo", "before": None, "after": {"read": True}}
    ]


def test_a_key_removed_from_a_jsonb_field_reads_as_after_null() -> None:
    before = {"narrowing": {"odoo": {"read": True}}}
    after: dict[str, Any] = {"narrowing": {}}
    assert diff_payloads(before, after) == [
        {"field": "narrowing.odoo", "before": {"read": True}, "after": None}
    ]


def test_a_dict_replaced_by_a_scalar_is_one_top_level_entry() -> None:
    """Only descends when BOTH sides are dicts. A type change is a change to
    the field itself, not to keys inside a thing that is no longer a dict."""
    assert diff_payloads({"definition": {"a": 1}}, {"definition": None}) == [
        {"field": "definition", "before": {"a": 1}, "after": None}
    ]


def test_a_list_field_is_compared_whole() -> None:
    """`skill_assignments` / `knowledge_grants` / `narrowing_overridden_keys`
    are lists. Diffing "one level deep" into a list would mean diffing by
    INDEX, which renders an insertion at the front as every element changing."""
    before = {"knowledge_grants": ["a", "b"]}
    after = {"knowledge_grants": ["a"]}
    assert diff_payloads(before, after) == [
        {"field": "knowledge_grants", "before": ["a", "b"], "after": ["a"]}
    ]


def test_the_reserved_meta_key_never_appears_in_a_diff() -> None:
    """`_meta` carries publish provenance, not configuration. A rollback would
    otherwise render as "the metadata changed" on top of the real change."""
    before = {"mission": "a", "_meta": {"rolled_back_from": 1}}
    after = {"mission": "a"}
    assert diff_payloads(before, after) == []


def test_entries_are_ordered_by_field_name() -> None:
    """Deterministic output: the Review dialog and any future compliance
    report render the same list in the same order, and a test asserting on it
    does not depend on dict insertion order."""
    before = {"role_title": "x", "mission": "a", "definition": {"k": 1}}
    after = {"role_title": "y", "mission": "b", "definition": {"k": 2}}
    assert [e["field"] for e in diff_payloads(before, after)] == [
        "definition.k",
        "mission",
        "role_title",
    ]


def test_changed_fields_is_the_entry_names() -> None:
    """What "N unpublished changes" counts. Granular on purpose: two threshold
    edits on two connections read as two changes, not as one "narrowing"."""
    before = {"mission": "a", "narrowing": {"odoo": {}, "github": {}}}
    after = {"mission": "b", "narrowing": {"odoo": {"read": True}, "github": {}}}
    assert changed_fields(before, after) == ["mission", "narrowing.odoo"]
