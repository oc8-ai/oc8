"""Governed components: a small, core-owned catalogue of generic layouts an
agent may render in its run transcript instead of only prose (see
docs/superpowers/plans/2026-08-21-governed-components.md).

Core ships the LAYOUT only -- a title, an optional subtitle, a handful of
label/value pairs, an optional link. What goes INSIDE one is whatever the
agent already has from an ordinary, already-authorized MCP tool call earlier
in the same run: this module never fetches anything and names no vendor,
matching the rule the rest of oc8.agent.control_tools already holds. Every
layout here is available to every agent: components are presentation only, so
there is no per-agent grant (the legacy component_grant table is no longer
consulted).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RecordCardField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(..., max_length=200)
    value: str = Field(..., max_length=500)


class RecordCardProps(BaseModel):
    """Props for the "record_card" layout: one entity, its key facts, an
    optional link back to the source system it came from."""

    model_config = ConfigDict(extra="forbid")
    title: str = Field(..., max_length=200)
    subtitle: str | None = Field(default=None, max_length=200)
    fields: list[RecordCardField] = Field(default_factory=list, max_length=20)
    link_label: str | None = Field(default=None, max_length=200)
    link_url: str | None = Field(default=None, max_length=2048)


class DataTableColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(..., max_length=100)
    label: str = Field(..., max_length=200)


class DataTableProps(BaseModel):
    """Props for the "data_table" layout: a multi-row grid, e.g. a daily
    report (timesheet summary, ticket backlog). `rows` keys must match a
    `columns[].key` -- extra/missing keys just render as an empty cell,
    since this is display data, not something re-parsed downstream."""

    model_config = ConfigDict(extra="forbid")
    title: str = Field(..., max_length=200)
    columns: list[DataTableColumn] = Field(..., min_length=1, max_length=20)
    rows: list[dict[str, str]] = Field(default_factory=list, max_length=500)
    caption: str | None = Field(default=None, max_length=500)


class ChartSeries(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., max_length=100)
    values: list[float] = Field(..., max_length=100)


class ChartProps(BaseModel):
    """Props shared by "bar_chart" and "line_chart" -- one category axis
    (`labels`) and one or more numeric series plotted against it."""

    model_config = ConfigDict(extra="forbid")
    title: str = Field(..., max_length=200)
    labels: list[str] = Field(..., min_length=1, max_length=100)
    series: list[ChartSeries] = Field(..., min_length=1, max_length=10)


#: The whole catalogue. A component_key not in here does not exist -- the
#: first of the two checks render_component makes (exists / valid props).
#: Every entry here is available to every agent; nothing is staged here that
#: isn't meant to be used yet.
COMPONENT_CATALOG: dict[str, type[BaseModel]] = {
    "record_card": RecordCardProps,
    "data_table": DataTableProps,
    "bar_chart": ChartProps,
    "line_chart": ChartProps,
}
