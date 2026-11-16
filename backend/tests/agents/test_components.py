from __future__ import annotations

import pytest
from pydantic import ValidationError

from oc8.agent.components import COMPONENT_CATALOG, ChartProps, DataTableProps, RecordCardProps


def test_record_card_is_in_the_catalog() -> None:
    assert COMPONENT_CATALOG["record_card"] is RecordCardProps


def test_data_table_is_in_the_catalog() -> None:
    assert COMPONENT_CATALOG["data_table"] is DataTableProps


def test_bar_and_line_chart_are_in_the_catalog() -> None:
    assert COMPONENT_CATALOG["bar_chart"] is ChartProps
    assert COMPONENT_CATALOG["line_chart"] is ChartProps


def test_data_table_accepts_its_documented_shape() -> None:
    props = DataTableProps.model_validate(
        {
            "title": "Timesheets — Woche 34",
            "columns": [
                {"key": "name", "label": "Mitarbeiter"},
                {"key": "hours", "label": "Stunden"},
            ],
            "rows": [
                {"name": "Anna M.", "hours": "38.5"},
                {"name": "Ben K.", "hours": "40.0"},
            ],
            "caption": "Quelle: Odoo Timesheets",
        }
    )
    assert len(props.rows) == 2
    assert props.rows[0]["hours"] == "38.5"


def test_data_table_requires_at_least_one_column() -> None:
    with pytest.raises(ValidationError):
        DataTableProps.model_validate({"title": "x", "columns": []})


def test_chart_accepts_its_documented_shape() -> None:
    props = ChartProps.model_validate(
        {
            "title": "Stunden pro Woche",
            "labels": ["KW32", "KW33", "KW34"],
            "series": [{"name": "Team A", "values": [38.5, 40.0, 35.0]}],
        }
    )
    assert props.series[0].values == [38.5, 40.0, 35.0]


def test_chart_requires_at_least_one_series() -> None:
    with pytest.raises(ValidationError):
        ChartProps.model_validate({"title": "x", "labels": ["a"], "series": []})


def test_record_card_accepts_its_documented_shape() -> None:
    props = RecordCardProps.model_validate(
        {
            "title": "Acme GmbH — 12.400 €",
            "subtitle": "Verhandlung",
            "fields": [{"label": "Abschluss erwartet", "value": "2026-09-01"}],
            "link_label": "In CRM öffnen",
            "link_url": "https://crm.example.com/deals/42",
        }
    )
    assert props.title == "Acme GmbH — 12.400 €"
    assert props.fields[0].label == "Abschluss erwartet"


def test_record_card_rejects_an_unknown_field() -> None:
    """extra='forbid': a model that starts inventing new prop keys must be
    told, not silently ignored -- an ignored key could carry a value nobody
    ever sees, which is worse than an outright error."""
    with pytest.raises(ValidationError):
        RecordCardProps.model_validate({"title": "x", "unexpected": "y"})


def test_record_card_requires_a_title() -> None:
    with pytest.raises(ValidationError):
        RecordCardProps.model_validate({"fields": []})


def test_data_table_accepts_plain_string_columns() -> None:
    props = DataTableProps.model_validate(
        {"title": "Angebote", "columns": ["Kunde", {"key": "sum", "label": "Summe"}]}
    )
    assert props.model_dump()["columns"] == [
        {"key": "Kunde", "label": "Kunde"},
        {"key": "sum", "label": "Summe"},
    ]


def test_data_table_coerces_scalar_row_values_to_text() -> None:
    props = DataTableProps.model_validate(
        {
            "title": "Angebote",
            "columns": ["Kunde", "Summe", "Anzahl", "Offen", "Notiz"],
            "rows": [{"Kunde": "A", "Summe": 269.1, "Anzahl": 3, "Offen": True, "Notiz": None}],
        }
    )
    assert props.rows == [
        {"Kunde": "A", "Summe": "269.1", "Anzahl": "3", "Offen": "true", "Notiz": ""}
    ]


def test_data_table_still_rejects_nested_row_values() -> None:
    with pytest.raises(ValidationError):
        DataTableProps.model_validate(
            {"title": "x", "columns": ["a"], "rows": [{"a": {"nested": 1}}]}
        )
