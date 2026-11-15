import { ChevronDown } from "lucide-react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Switch } from "@/components/ui/switch";
import { useBoardQuery } from "@/lib/boards-api";
import type {
  BoardSpec,
  CategoryFilterRef,
  Comparison,
  FilterBarBlock,
  Preset,
} from "@/lib/boards-api";
import { useT } from "@/lib/i18n";
import type { Translate } from "@/lib/tool-labels";

import {
  addDays,
  categoryValues,
  categoryValuesQuery,
  presetPeriod,
  withCustomFrom,
  withCustomLastDay,
  type BoardFilterState,
} from "./board-filters";

const FIELD_CLASS =
  "rounded-md border border-border bg-panel px-2.5 py-1 text-xs outline-none focus:border-primary/50";
const LABEL_CLASS =
  "flex flex-col gap-1 text-[10px] uppercase tracking-wider text-muted-foreground";

const ALL_PRESETS: Preset[] = ["this_month", "last_12_months", "year_to_date", "custom"];
const ALL_COMPARISONS: Comparison[] = ["previous_period", "same_period_last_year"];

function presetLabel(p: Preset, t: Translate): string {
  switch (p) {
    case "this_month":
      return t("This month", "Dieser Monat");
    case "last_12_months":
      return t("Last 12 months", "Letzte 12 Monate");
    case "year_to_date":
      return t("Year to date", "Seit Jahresbeginn");
    case "custom":
      return t("Custom range", "Eigener Zeitraum");
  }
}

function compareLabel(c: BoardFilterState["compare"], t: Translate): string {
  switch (c) {
    case "none":
      return t("No comparison", "Kein Vergleich");
    case "previous_period":
      return t("vs. previous period", "ggü. Vorperiode");
    case "same_period_last_year":
      return t("vs. same period last year", "ggü. Vorjahreszeitraum");
  }
}

/** Period, comparison, reporting and category filters for the whole board. */
export function FilterBar({
  boardId,
  block,
  spec,
  filters,
  onFiltersChange,
  today,
}: {
  boardId: string;
  block: FilterBarBlock;
  spec: BoardSpec;
  filters: BoardFilterState;
  onFiltersChange: (s: BoardFilterState) => void;
  today: Date;
}) {
  const t = useT();
  const presets = block.presets ?? ALL_PRESETS;
  const comparisons = block.comparisons ?? ALL_COMPARISONS;
  // One control per category field: the same key on two datasets is one
  // selection, applied to each dataset the bar names it for.
  const refs = (block.category_filters ?? []).filter(
    (r, i, all) => all.findIndex((o) => o.field === r.field) === i,
  );

  const setPreset = (preset: Preset) => {
    if (preset === "custom") {
      const { from, to } = presetPeriod(filters, today);
      onFiltersChange({ ...filters, preset, from, to });
    } else {
      const { from: _from, to: _to, ...rest } = filters;
      onFiltersChange({ ...rest, preset });
    }
  };

  return (
    <div className="flex flex-wrap items-end gap-3 px-3 py-2">
      <label className={LABEL_CLASS}>
        {t("Period", "Zeitraum")}
        <select
          aria-label={t("Period", "Zeitraum")}
          value={filters.preset}
          onChange={(e) => setPreset(e.target.value as Preset)}
          className={FIELD_CLASS}
        >
          {presets.map((p) => (
            <option key={p} value={p}>
              {presetLabel(p, t)}
            </option>
          ))}
        </select>
      </label>

      {filters.preset === "custom" && (
        <>
          <label className={LABEL_CLASS}>
            {t("From", "Von")}
            <input
              type="date"
              aria-label={t("From", "Von")}
              value={filters.from ?? ""}
              onChange={(e) =>
                e.target.value && onFiltersChange(withCustomFrom(filters, e.target.value))
              }
              className={FIELD_CLASS}
            />
          </label>
          <label className={LABEL_CLASS}>
            {t("To", "Bis")}
            {/* The period ends before `to`; the picker shows the last day in it. */}
            <input
              type="date"
              aria-label={t("To", "Bis")}
              value={filters.to ? addDays(filters.to, -1) : ""}
              onChange={(e) =>
                e.target.value && onFiltersChange(withCustomLastDay(filters, e.target.value))
              }
              className={FIELD_CLASS}
            />
          </label>
        </>
      )}

      {comparisons.length > 0 && (
        <label className={LABEL_CLASS}>
          {t("Compare", "Vergleich")}
          <select
            aria-label={t("Compare", "Vergleich")}
            value={filters.compare}
            onChange={(e) =>
              onFiltersChange({
                ...filters,
                compare: e.target.value as BoardFilterState["compare"],
              })
            }
            className={FIELD_CLASS}
          >
            {(["none", ...comparisons] as const).map((c) => (
              <option key={c} value={c}>
                {compareLabel(c, t)}
              </option>
            ))}
          </select>
        </label>
      )}

      {refs.map((ref) => (
        <CategoryFilter
          key={ref.field}
          boardId={boardId}
          spec={spec}
          refSpec={ref}
          filters={filters}
          onFiltersChange={onFiltersChange}
          today={today}
        />
      ))}

      <label className="flex items-center gap-2 pb-1 text-xs text-muted-foreground">
        <Switch
          aria-label={t("As first reported", "Wie zuerst gemeldet")}
          checked={filters.reported === "first"}
          onCheckedChange={(on) =>
            onFiltersChange({ ...filters, reported: on ? "first" : "latest" })
          }
        />
        <span aria-hidden>{t("As first reported", "Wie zuerst gemeldet")}</span>
      </label>
    </div>
  );
}

function CategoryFilter({
  boardId,
  spec,
  refSpec,
  filters,
  onFiltersChange,
  today,
}: {
  boardId: string;
  spec: BoardSpec;
  refSpec: CategoryFilterRef;
  filters: BoardFilterState;
  onFiltersChange: (s: BoardFilterState) => void;
  today: Date;
}) {
  const t = useT();
  const { data, error } = useBoardQuery(
    boardId,
    categoryValuesQuery(spec, refSpec, filters, today),
  );
  const label =
    spec.datasets
      .find((d) => d.key === refSpec.dataset)
      ?.fields.find((f) => f.key === refSpec.field)?.label ?? refSpec.field;
  const selected = filters.categories[refSpec.field] ?? [];
  // A selected value outside this period still shows, so it can be cleared.
  const values = [...new Set([...categoryValues(data, refSpec.field), ...selected])].sort((a, b) =>
    a.localeCompare(b),
  );

  const valueText =
    selected.length === 0
      ? t("All", "Alle")
      : selected.length === 1
        ? selected[0]
        : t("{n} selected", "{n} ausgewählt").replace("{n}", String(selected.length));

  const toggle = (value: string, on: boolean) => {
    const next = on ? [...selected, value] : selected.filter((v) => v !== value);
    const categories = { ...filters.categories, [refSpec.field]: next };
    if (next.length === 0) delete categories[refSpec.field];
    onFiltersChange({ ...filters, categories });
  };

  return (
    <div className={LABEL_CLASS}>
      <span aria-hidden>{label}</span>
      <Popover>
        <PopoverTrigger asChild>
          <button
            type="button"
            aria-label={`${label}: ${valueText}`}
            className={`${FIELD_CLASS} inline-flex items-center gap-1.5 normal-case tracking-normal text-foreground`}
          >
            {valueText}
            <ChevronDown aria-hidden className="size-3 text-muted-foreground" />
          </button>
        </PopoverTrigger>
        <PopoverContent align="start" className="w-56 p-2">
          {values.length === 0 ? (
            <p className="px-1 py-1 text-xs text-muted-foreground">
              {error
                ? t("Values couldn't be loaded", "Werte konnten nicht geladen werden")
                : data
                  ? t("No values in this period", "Keine Werte in diesem Zeitraum")
                  : t("Loading…", "Wird geladen…")}
            </p>
          ) : (
            <ul className="flex max-h-64 flex-col gap-0.5 overflow-y-auto">
              {values.map((v) => (
                <li key={v}>
                  <label className="flex cursor-pointer items-center gap-2 rounded px-1 py-1 text-xs hover:bg-muted/40">
                    <input
                      type="checkbox"
                      checked={selected.includes(v)}
                      onChange={(e) => toggle(v, e.target.checked)}
                    />
                    <span className="truncate">{v}</span>
                  </label>
                </li>
              ))}
            </ul>
          )}
        </PopoverContent>
      </Popover>
    </div>
  );
}
