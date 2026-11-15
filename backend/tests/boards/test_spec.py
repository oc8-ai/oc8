"""Board spec schema, value validation and the additive rule (pure unit tests)."""

from __future__ import annotations

import copy
import datetime as dt
from typing import Any

import pytest

from oc8.boards.spec import (
    BoardSpecInvalid,
    DatasetSpec,
    SpecChangeRefused,
    check_additive,
    normalize_as_of,
    parse_board_spec,
    uses_artifact,
    validate_values,
)


def _spec() -> dict[str, Any]:
    return {
        "datasets": [
            {
                "key": "sales",
                "label": "Sales",
                "grain": "day",
                "fields": [
                    {"key": "amount", "label": "Amount", "type": "money", "currency": "EUR"},
                    {"key": "region", "label": "Region", "type": "category"},
                ],
            }
        ],
        "blocks": [{"type": "kpi", "dataset": "sales", "field": "amount"}],
    }


def _sales() -> DatasetSpec:
    return parse_board_spec(_spec()).datasets[0]


def test_a_minimal_spec_parses() -> None:
    spec = parse_board_spec(_spec())
    assert spec.datasets[0].key == "sales"
    assert spec.blocks[0].width == 6
    assert not uses_artifact(spec)


def test_unknown_keys_are_refused() -> None:
    raw = _spec() | {"colour": "red"}
    with pytest.raises(BoardSpecInvalid) as exc:
        parse_board_spec(raw)
    assert "colour" in str(exc.value)
    assert exc.value.code == "board_spec_invalid"


def test_non_object_is_refused() -> None:
    with pytest.raises(BoardSpecInvalid):
        parse_board_spec("nope")


def test_money_requires_a_currency_and_others_forbid_it() -> None:
    raw = _spec()
    del raw["datasets"][0]["fields"][0]["currency"]
    with pytest.raises(BoardSpecInvalid, match="amount"):
        parse_board_spec(raw)
    raw = _spec()
    raw["datasets"][0]["fields"][1]["currency"] = "EUR"
    with pytest.raises(BoardSpecInvalid, match="region"):
        parse_board_spec(raw)
    raw = _spec()
    raw["datasets"][0]["fields"][0]["currency"] = "eur"
    with pytest.raises(BoardSpecInvalid):
        parse_board_spec(raw)


def test_block_must_reference_an_existing_numeric_field() -> None:
    raw = _spec()
    raw["blocks"] = [{"type": "kpi", "dataset": "sales", "field": "region"}]
    with pytest.raises(BoardSpecInvalid, match="region"):
        parse_board_spec(raw)
    raw["blocks"] = [{"type": "kpi", "dataset": "sales", "field": "ghost"}]
    with pytest.raises(BoardSpecInvalid, match="ghost"):
        parse_board_spec(raw)
    raw["blocks"] = [{"type": "kpi", "dataset": "nope", "field": "amount"}]
    with pytest.raises(BoardSpecInvalid, match="nope"):
        parse_board_spec(raw)


def test_chart_split_by_must_be_a_category() -> None:
    raw = _spec()
    raw["blocks"] = [
        {"type": "line", "dataset": "sales", "fields": ["amount"], "split_by": "region"}
    ]
    assert parse_board_spec(raw).blocks[0].type == "line"
    raw["blocks"][0]["split_by"] = "amount"
    with pytest.raises(BoardSpecInvalid, match="amount"):
        parse_board_spec(raw)


def test_only_one_filter_bar() -> None:
    raw = _spec()
    raw["blocks"] = [{"type": "filter_bar"}]
    spec = parse_board_spec(raw)
    assert spec.blocks[0].width == 12
    raw["blocks"] = [{"type": "filter_bar"}, {"type": "filter_bar"}]
    with pytest.raises(BoardSpecInvalid, match="filter_bar"):
        parse_board_spec(raw)


def test_filter_bar_category_filters_must_be_categories() -> None:
    raw = _spec()
    raw["blocks"] = [
        {"type": "filter_bar", "category_filters": [{"dataset": "sales", "field": "amount"}]}
    ]
    with pytest.raises(BoardSpecInvalid, match="amount"):
        parse_board_spec(raw)


def test_artifact_block_contract() -> None:
    raw = _spec()
    raw["blocks"] = [{"type": "artifact", "artifact_ref": "x1", "datasets": ["sales"]}]
    spec = parse_board_spec(raw)
    assert uses_artifact(spec)
    raw["blocks"] = [{"type": "artifact", "artifact_ref": "x1", "datasets": ["nope"]}]
    with pytest.raises(BoardSpecInvalid, match="nope"):
        parse_board_spec(raw)


@pytest.mark.parametrize(
    "value", ["12", True, float("nan"), float("inf"), 10**400, 1e308, -(10**16)]
)
def test_values_are_validated_against_field_types(value: object) -> None:
    with pytest.raises(BoardSpecInvalid) as exc:
        validate_values(_sales(), {"amount": value})
    assert "amount" in str(exc.value)
    assert exc.value.code == "invalid_value"


def test_valid_values_pass_and_missing_keys_stay_absent() -> None:
    assert validate_values(_sales(), {"amount": 12.5}) == {"amount": 12.5}
    assert validate_values(_sales(), {"region": "north"}) == {"region": "north"}
    assert validate_values(_sales(), {}) == {}


def test_text_category_and_date_values() -> None:
    raw = _spec()
    raw["datasets"][0]["fields"] += [
        {"key": "memo", "label": "Memo", "type": "text"},
        {"key": "due", "label": "Due", "type": "date"},
    ]
    ds = parse_board_spec(raw).datasets[0]
    assert validate_values(ds, {"due": "2026-03-18"}) == {"due": "2026-03-18"}
    for bad in ({"due": "18.03.2026"}, {"memo": "x" * 2001}, {"region": "x" * 201}, {"memo": 3}):
        with pytest.raises(BoardSpecInvalid):
            validate_values(ds, bad)


def test_unknown_value_key_is_refused() -> None:
    with pytest.raises(BoardSpecInvalid, match="amout"):
        validate_values(_sales(), {"amout": 1})


def test_as_of_is_normalized_to_the_grain() -> None:
    assert normalize_as_of("2026-03-18", "day") == dt.datetime(2026, 3, 18, tzinfo=dt.UTC)
    assert normalize_as_of("2026-03-18T07:30:00", "day") == dt.datetime(2026, 3, 18, tzinfo=dt.UTC)
    assert normalize_as_of("2026-03-18", "week") == dt.datetime(2026, 3, 16, tzinfo=dt.UTC)
    assert normalize_as_of("2026-03-18", "month") == dt.datetime(2026, 3, 1, tzinfo=dt.UTC)
    assert normalize_as_of("2026-03-18T07:30:00", "moment") == dt.datetime(
        2026, 3, 18, 7, 30, tzinfo=dt.UTC
    )
    with pytest.raises(BoardSpecInvalid):
        normalize_as_of("March", "day")


def _change_field(raw: dict[str, Any], idx: int, **kw: object) -> None:
    raw["datasets"][0]["fields"][idx].update(kw)


def _remove_field(raw: dict[str, Any]) -> None:
    del raw["datasets"][0]["fields"][1]
    raw["blocks"] = [{"type": "kpi", "dataset": "sales", "field": "amount"}]


def _rename_field(raw: dict[str, Any]) -> None:
    _change_field(raw, 1, key="area")


def _drop_dataset(raw: dict[str, Any]) -> None:
    raw["datasets"][0]["key"] = "other"
    raw["blocks"][0]["dataset"] = "other"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (_remove_field, "field_removed"),
        (_rename_field, "field_removed"),
        (lambda r: _change_field(r, 1, type="text"), "field_type_changed"),
        (lambda r: r["datasets"][0].update(grain="month"), "grain_changed"),
        (lambda r: _change_field(r, 0, currency="USD"), "currency_changed"),
        (_drop_dataset, "dataset_removed"),
    ],
)
def test_board_update_is_additive(change: Any, code: str) -> None:
    old = parse_board_spec(_spec())
    raw = copy.deepcopy(_spec())
    change(raw)
    new = parse_board_spec(raw)
    with pytest.raises(SpecChangeRefused) as exc:
        check_additive(old, new)
    assert exc.value.code == code


def test_labels_blocks_and_new_fields_may_change() -> None:
    old = parse_board_spec(_spec())
    raw = _spec()
    raw["datasets"][0]["label"] = "Revenue"
    raw["datasets"][0]["fields"][0]["label"] = "Total"
    raw["datasets"][0]["fields"].append({"key": "n", "label": "N", "type": "number"})
    raw["datasets"].append(
        {
            "key": "costs",
            "label": "Costs",
            "grain": "month",
            "fields": [{"key": "c", "label": "C", "type": "number"}],
        }
    )
    raw["blocks"] = [{"type": "table", "dataset": "costs", "fields": ["c"]}]
    check_additive(old, parse_board_spec(raw))


def test_errors_do_not_echo_submitted_text() -> None:
    raw = _spec()
    raw["blocks"] = [{"type": "SENTINEL_TYPE", "dataset": "sales"}]
    with pytest.raises(BoardSpecInvalid) as exc:
        parse_board_spec(raw)
    assert "SENTINEL_TYPE" not in str(exc.value)
    assert "unknown block type" in str(exc.value)


def test_long_extra_keys_and_refs_are_truncated_or_not_echoed() -> None:
    raw = _spec() | {"k" * 500: 1}
    with pytest.raises(BoardSpecInvalid) as exc:
        parse_board_spec(raw)
    assert "k" * 41 not in str(exc.value)
    raw = _spec()
    raw["blocks"] = [{"type": "kpi", "dataset": "SENTINEL" * 50, "field": "amount"}]
    with pytest.raises(BoardSpecInvalid) as exc:
        parse_board_spec(raw)
    assert "SENTINEL" not in str(exc.value)
    assert len(str(exc.value)) < 1100
    with pytest.raises(BoardSpecInvalid) as exc:
        validate_values(_sales(), {"z" * 500: 1})
    assert "z" * 41 not in str(exc.value)


def test_as_of_overflow_is_refused() -> None:
    with pytest.raises(BoardSpecInvalid):
        normalize_as_of("0001-01-01T00:00:00+05:00", "day")


def test_filter_bar_choices_are_unique_and_page_size_is_strict() -> None:
    raw = _spec()
    raw["blocks"] = [{"type": "filter_bar", "presets": ["custom", "custom"]}]
    with pytest.raises(BoardSpecInvalid):
        parse_board_spec(raw)
    raw["blocks"] = [{"type": "table", "dataset": "sales", "fields": ["amount"], "page_size": True}]
    with pytest.raises(BoardSpecInvalid):
        parse_board_spec(raw)


def test_a_change_on_a_second_dataset_is_refused() -> None:
    base = _spec()
    base["datasets"].append(
        {
            "key": "costs",
            "label": "Costs",
            "grain": "month",
            "fields": [{"key": "c", "label": "C", "type": "number"}],
        }
    )
    old = parse_board_spec(base)
    raw = copy.deepcopy(base)
    raw["datasets"][1]["grain"] = "day"
    with pytest.raises(SpecChangeRefused) as exc:
        check_additive(old, parse_board_spec(raw))
    assert exc.value.code == "grain_changed"
