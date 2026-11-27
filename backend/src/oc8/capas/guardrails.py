"""`guardrails/*.toml` library entries (design §3-4): a plugin's own library of
named, documented use-case guardrails, kept out of `plugin.toml` so the
manifest stays a readable teaching piece while the library becomes the
reference work. (`parse_guardrails_toml` below still parses the pre-restructure
single-file shape directly, for callers -- today, only its own tests -- that
have a whole library as one TOML document rather than one `guardrails/<key>.toml`
file per entry; `discovery.py`'s real assembly path is `parse_guardrail_entry`.)

A guardrail is data that produces an ordinary tool policy -- applying one
writes `read/write/send/approval_eur/approval_actions/only` into a department
frame, exactly as `GuardrailPreset` (`plugins/manifest.py`) does today.
`authz/pdp.py` never learns that a guardrail library exists.

Unlike `GuardrailPreset.approval_actions`, this model's `approval_actions` is
NOT restricted to `RIGHTS` -- Task 1 (`authz/pdp.py::authorize_tool`) taught
the PDP to match `approval_actions` against a tool key as well as a right, so
an entry naming a plugin-defined tool (e.g. "post_message") is legitimate
here. This parser has no access to the plugin's real tool list, so it stays
permissive on the string itself; it only rejects the empty string, which can
never name anything.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from oc8.authz.pdp import UsageLimit
from oc8.capas.manifest import ManifestError, check_limits_within_only, parse_usage_limits

#: Fields whose value, when numeric, is subject to the "min/max must bracket
#: the shipped default" rule. Kept as a set of attribute names rather than
#: inspecting every field generically so a future non-numeric field can never
#: be silently pulled into a bracket check.
_NUMERIC_ADJUSTABLE_FIELDS = ("approval_eur",)


class GuardrailAdjustable(BaseModel):
    """One number (or short list) on a `Guardrail` the wizard lets an
    operator change before applying it. Fields not listed here are not
    offered for editing -- the guardrail author decides which numbers are the
    operator's business."""

    model_config = ConfigDict(extra="forbid")
    field: str
    label: str
    unit: str | None = None
    min: float | int | None = None
    max: float | int | None = None


class Guardrail(BaseModel):
    """A named, documented ERP scenario with a policy and adjustable
    numbers -- more than a bare permission ceiling (design §4)."""

    model_config = ConfigDict(extra="forbid")
    key: str
    label: str
    summary: str
    #: Groups the library (`sales`, `helpdesk`, `purchasing`, ...). A plain
    #: string, not an enum: a plugin for a system oc8 has never seen must be
    #: able to name its own domains.
    use_case: str
    read: bool = False
    modify: bool = False
    approval_eur: float | None = None
    approval_actions: frozenset[str] = frozenset()
    #: The ONLY tool names this guardrail puts within reach; empty means all
    #: of the connection's tools. See `GuardrailPreset.only` for the full
    #: rationale (a euro threshold cannot gate a deletion).
    only: tuple[str, ...] = ()
    #: Usage limits, as on `GuardrailPreset.limits`.
    limits: list[UsageLimit] = []
    adjustable: list[GuardrailAdjustable] = []

    @field_validator("limits", mode="before")
    @classmethod
    def _parse_limits(cls, v: Any) -> list[UsageLimit]:
        return parse_usage_limits(v)

    @field_validator("approval_eur", mode="before")
    @classmethod
    def _empty_string_means_no_threshold(cls, v: Any) -> Any:
        # TOML has no null; manifests use "" for "no threshold". `0` is kept
        # as-is -- it means "a human decides every send", and treating it as
        # falsy would silently delete the strictest setting.
        if v == "":
            return None
        return v

    @field_validator("approval_actions", mode="before")
    @classmethod
    def _reject_empty_action_entries(cls, v: Any) -> Any:
        if v is None:
            return v
        if any(a == "" for a in v):
            raise ValueError("guardrail approval_actions may not contain an empty-string entry")
        return v

    @model_validator(mode="after")
    def _validate_adjustable_fields(self) -> Guardrail:
        check_limits_within_only(self.key, self.limits, self.only)
        known_fields = type(self).model_fields.keys()
        for adj in self.adjustable:
            if adj.field not in known_fields:
                raise ValueError(
                    f"guardrail '{self.key}' adjustable names unknown field '{adj.field}'"
                )
            if adj.field not in _NUMERIC_ADJUSTABLE_FIELDS:
                continue
            default = getattr(self, adj.field)
            if default is None:
                # No shipped value to bracket -- e.g. approval_eur left unset.
                continue
            if adj.min is not None and default < adj.min:
                raise ValueError(
                    f"guardrail '{self.key}' adjustable '{adj.field}' has min={adj.min} "
                    f"which excludes its own shipped default {default}"
                )
            if adj.max is not None and default > adj.max:
                raise ValueError(
                    f"guardrail '{self.key}' adjustable '{adj.field}' has max={adj.max} "
                    f"which excludes its own shipped default {default}"
                )
        return self


class GuardrailLibrary(BaseModel):
    """The parsed contents of one plugin's guardrail library -- either the
    pre-restructure single `guardrails.toml` file (`parse_guardrails_toml`) or
    the `kind = "library"` entries assembled from `guardrails/*.toml`
    (`discovery.py`'s `_read_guardrails_folder` + `parse_guardrail_entry`)."""

    model_config = ConfigDict(extra="forbid")
    guardrail: list[Guardrail] = []

    @model_validator(mode="after")
    def _no_duplicate_keys(self) -> GuardrailLibrary:
        keys = [g.key for g in self.guardrail]
        seen: set[str] = set()
        dupes: set[str] = set()
        for k in keys:
            if k in seen:
                dupes.add(k)
            seen.add(k)
        if dupes:
            names = ", ".join(sorted(dupes))
            raise ValueError(f"guardrail library has duplicate key(s): {names}")
        return self


def parse_guardrails_toml(path: Path) -> GuardrailLibrary | None:
    """Parse a guardrail-library-shaped TOML document: one file holding the
    whole `guardrail` array, the pre-restructure single-file shape. A live
    plugin folder never has a bare `guardrails.toml` any more -- `discovery.py`
    treats one as a hard error naming the `guardrails/<key>.toml` split it
    moved to -- so this function today serves only its own direct tests and
    any caller parsing that shape from an arbitrary path; the per-plugin
    assembly path is `parse_guardrail_entry`, one call per
    `guardrails/<key>.toml` file.

    Returns `None` if `path` does not exist -- a plugin without one behaves
    exactly as today (design §3.1). A malformed file raises `ManifestError`
    naming `path`, matching how `plugin.toml` errors already surface through
    `discovery.py`.
    """
    if not path.exists():
        return None
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"{path}: invalid TOML: {exc}") from exc
    try:
        return GuardrailLibrary.model_validate(data)
    except ValidationError as exc:
        raise ManifestError(f"{path}: {exc}") from exc


def parse_guardrail_entry(data: dict[str, Any]) -> Guardrail:
    """Parse one `guardrails/<key>.toml` file's content (with `kind` already
    stripped by the caller) into a `Guardrail`. Raises `ManifestError` on any
    validation failure, matching `parse_guardrails_toml`'s own error shape."""
    try:
        return Guardrail.model_validate(data)
    except ValidationError as exc:
        raise ManifestError(str(exc)) from exc
