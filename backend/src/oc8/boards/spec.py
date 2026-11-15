"""The closed schema of a data board and the rules for writing to it.

A board's spec declares datasets (typed fields and a grain) and the catalogue
blocks that render them. Unknown keys are refused everywhere. Messages are
built from keys and expected types, never from submitted values.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Mapping
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

KEY_PATTERN = r"^[a-z][a-z0-9_]{0,39}$"
NUMERIC_TYPES: frozenset[str] = frozenset({"number", "money", "percent"})

_KEY = Field(pattern=KEY_PATTERN)
Ref = Annotated[str, Field(pattern=KEY_PATTERN)]
_SEG_MAX = 40
_MESSAGE_MAX = 1000
# Magnitude bound for stored numbers: integers up to 1e15 are exact in float64
# (2**53 is about 9e15) and leave headroom for a SUM over many rows.
MAX_ABS_VALUE = 10**15
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

FieldType = Literal["number", "money", "percent", "text", "category", "date"]
Grain = Literal["day", "week", "month", "moment"]
Width = Literal[3, 4, 6, 8, 12]
Preset = Literal["this_month", "last_12_months", "year_to_date", "custom"]
Comparison = Literal["previous_period", "same_period_last_year"]


class BoardSpecInvalid(ValueError):
    """The spec or the values are not valid.

    `code` is a stable reason code; the message may name submitted keys and is
    meant for the model that wrote them.
    """

    def __init__(self, message: str, code: str = "board_spec_invalid") -> None:
        super().__init__(message)
        self.code = code


class SpecChangeRefused(ValueError):
    """A board update that is not purely additive."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldSpec(_Strict):
    key: str = _KEY
    label: str = Field(max_length=100)
    type: FieldType
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def _currency_rule(self) -> FieldSpec:
        if self.type == "money" and self.currency is None:
            raise ValueError(f"field '{self.key}' is money and needs a currency")
        if self.type != "money" and self.currency is not None:
            raise ValueError(f"field '{self.key}' is not money and must not have a currency")
        return self


class DatasetSpec(_Strict):
    key: str = _KEY
    label: str = Field(max_length=100)
    grain: Grain
    fields: list[FieldSpec] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def _unique_fields(self) -> DatasetSpec:
        keys = [f.key for f in self.fields]
        if len(set(keys)) != len(keys):
            raise ValueError(f"dataset '{self.key}' has duplicate field keys")
        return self

    def field(self, key: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.key == key), None)


class _Block(_Strict):
    title: str | None = Field(default=None, max_length=200)
    width: Width = 6


class KpiBlock(_Block):
    type: Literal["kpi"]
    dataset: Ref
    field: Ref


class ChartBlock(_Block):
    type: Literal["line", "bar", "area"]
    dataset: Ref
    fields: list[Ref] = Field(min_length=1, max_length=5)
    split_by: Ref | None = None


class TableBlock(_Block):
    type: Literal["table"]
    dataset: Ref
    fields: list[Ref] = Field(min_length=1, max_length=20)
    page_size: int = Field(default=25, ge=5, le=100, strict=True)


class CategoryFilterRef(_Strict):
    dataset: Ref
    field: Ref


class FilterBarBlock(_Block):
    type: Literal["filter_bar"]
    width: Literal[12] = 12
    presets: list[Preset] = Field(default_factory=lambda: list(get_args(Preset)), max_length=4)
    comparisons: list[Comparison] = Field(
        default_factory=lambda: list(get_args(Comparison)), max_length=2
    )
    category_filters: list[CategoryFilterRef] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _unique_choices(self) -> FilterBarBlock:
        if len(set(self.presets)) != len(self.presets):
            raise ValueError("filter_bar presets must be unique")
        if len(set(self.comparisons)) != len(self.comparisons):
            raise ValueError("filter_bar comparisons must be unique")
        return self


class NoteBlock(_Block):
    type: Literal["note"]
    dataset: Ref


class ArtifactBlock(_Block):
    type: Literal["artifact"]
    artifact_ref: str = Field(min_length=1, max_length=200)
    datasets: list[Ref] = Field(min_length=1, max_length=10)


Block = Annotated[
    KpiBlock | ChartBlock | TableBlock | FilterBarBlock | NoteBlock | ArtifactBlock,
    Field(discriminator="type"),
]


class BoardSpec(_Strict):
    datasets: list[DatasetSpec] = Field(min_length=1, max_length=10)
    blocks: list[Block] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def _references(self) -> BoardSpec:
        by_key = {d.key: d for d in self.datasets}
        if len(by_key) != len(self.datasets):
            raise ValueError("duplicate dataset keys")

        def dataset(key: str) -> DatasetSpec:
            found = by_key.get(key)
            if found is None:
                raise ValueError(f"unknown dataset '{key}'")
            return found

        def field(ds: str, key: str, *, numeric: bool = False, category: bool = False) -> None:
            f = dataset(ds).field(key)
            if f is None:
                raise ValueError(f"dataset '{ds}' has no field '{key}'")
            if numeric and f.type not in NUMERIC_TYPES:
                raise ValueError(f"field '{key}' of '{ds}' must be number, money or percent")
            if category and f.type != "category":
                raise ValueError(f"field '{key}' of '{ds}' must be a category")

        filter_bars = 0
        for b in self.blocks:
            if isinstance(b, KpiBlock):
                field(b.dataset, b.field, numeric=True)
            elif isinstance(b, ChartBlock):
                for k in b.fields:
                    field(b.dataset, k, numeric=True)
                if b.split_by is not None:
                    field(b.dataset, b.split_by, category=True)
            elif isinstance(b, TableBlock):
                for k in b.fields:
                    field(b.dataset, k)
            elif isinstance(b, FilterBarBlock):
                filter_bars += 1
                for ref in b.category_filters:
                    field(ref.dataset, ref.field, category=True)
            elif isinstance(b, NoteBlock):
                dataset(b.dataset)
            else:
                for ds in b.datasets:
                    dataset(ds)
        if filter_bars > 1:
            raise ValueError("at most one filter_bar block is allowed")
        return self


def _loc(loc: tuple[int | str, ...]) -> str:
    return ".".join(str(p)[:_SEG_MAX] for p in loc) or "spec"


def _describe(err_type: str, msg: str) -> str:
    if err_type in ("union_tag_invalid", "union_tag_not_found"):
        return "unknown block type"
    if err_type == "extra_forbidden":
        return "unknown key"
    return msg.removeprefix("Value error, ")


def parse_board_spec(raw: object) -> BoardSpec:
    """Validate a raw spec; raises BoardSpecInvalid with a corrigible message.

    The message is built from error types and truncated locations, never from
    submitted values.
    """
    if not isinstance(raw, Mapping):
        raise BoardSpecInvalid("the board spec must be an object")
    try:
        return BoardSpec.model_validate(dict(raw))
    except ValidationError as exc:
        parts = []
        for err in exc.errors(include_input=False, include_url=False, include_context=False):
            parts.append(f"{_loc(err['loc'])}: {_describe(err['type'], str(err['msg']))}")
        message = "invalid board spec: " + "; ".join(parts[:8])
        raise BoardSpecInvalid(message[:_MESSAGE_MAX]) from None


def uses_artifact(spec: BoardSpec) -> bool:
    return any(isinstance(b, ArtifactBlock) for b in spec.blocks)


def validate_values(dataset: DatasetSpec, row: Mapping[str, object]) -> dict[str, object]:
    """Check a row against the dataset's fields; missing keys stay absent."""
    out: dict[str, object] = {}
    for key, value in row.items():
        f = dataset.field(key)
        if f is None:
            raise BoardSpecInvalid(
                f"dataset '{dataset.key}' has no field '{key[:_SEG_MAX]}'", code="unknown_field"
            )
        if f.type in NUMERIC_TYPES:
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or (isinstance(value, float) and not math.isfinite(value))
                or abs(value) > MAX_ABS_VALUE
            ):
                raise BoardSpecInvalid(
                    f"field '{key}' must be a finite number of at most 1e15 in magnitude",
                    code="invalid_value",
                )
        elif f.type == "text":
            if not isinstance(value, str) or len(value) > 2000:
                raise BoardSpecInvalid(
                    f"field '{key}' must be text of at most 2000 characters", code="invalid_value"
                )
        elif f.type == "category":
            if not isinstance(value, str) or len(value) > 200:
                raise BoardSpecInvalid(
                    f"field '{key}' must be a category of at most 200 characters",
                    code="invalid_value",
                )
        else:
            ok = isinstance(value, str) and _DATE_RE.match(value) is not None
            if ok:
                try:
                    dt.date.fromisoformat(str(value))
                except ValueError:
                    ok = False
            if not ok:
                raise BoardSpecInvalid(
                    f"field '{key}' must be an ISO date (YYYY-MM-DD)", code="invalid_value"
                )
        out[key] = value
    return out


def normalize_as_of(raw: str, grain: str) -> dt.datetime:
    """Parse `raw` and snap it to the start of its grain period, in UTC."""
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except (ValueError, TypeError, OverflowError):
        raise BoardSpecInvalid(
            "as_of must be an ISO date or datetime", code="invalid_as_of"
        ) from None
    try:
        return _snap(parsed, grain)
    except OverflowError:
        raise BoardSpecInvalid(
            "as_of must be an ISO date or datetime", code="invalid_as_of"
        ) from None


def _snap(parsed: dt.datetime, grain: str) -> dt.datetime:
    parsed = parsed.replace(tzinfo=dt.UTC) if parsed.tzinfo is None else parsed.astimezone(dt.UTC)
    day = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
    if grain == "moment":
        return parsed
    if grain == "day":
        return day
    if grain == "week":
        return day - dt.timedelta(days=day.weekday())
    if grain == "month":
        return day.replace(day=1)
    raise BoardSpecInvalid("unknown grain", code="invalid_as_of")


def check_additive(old: BoardSpec, new: BoardSpec) -> None:
    """A board update may only add datasets and fields (spec 6.8 rule 3)."""
    new_ds = {d.key: d for d in new.datasets}
    for od in old.datasets:
        nd = new_ds.get(od.key)
        if nd is None:
            raise SpecChangeRefused("dataset_removed")
        if nd.grain != od.grain:
            raise SpecChangeRefused("grain_changed")
        for of in od.fields:
            nf = nd.field(of.key)
            if nf is None:
                raise SpecChangeRefused("field_removed")
            if nf.type != of.type:
                raise SpecChangeRefused("field_type_changed")
            if nf.currency != of.currency:
                raise SpecChangeRefused("currency_changed")
