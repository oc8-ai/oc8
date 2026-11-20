// Value and date formatting for dashboard blocks, in the reader's language.
// Money uses its field's currency (fixed per field, so a column never mixes
// currencies); a percent field holds percent points (12.5 means 12.5 %).
import { ApiError } from "@/lib/api";
import type { FieldSpec, Grain } from "@/lib/boards-api";
import type { Translate } from "@/lib/tool-labels";

export const NO_VALUE = "—";

function number(lang: string, options: Intl.NumberFormatOptions): Intl.NumberFormat {
  try {
    return new Intl.NumberFormat(lang, options);
  } catch {
    return new Intl.NumberFormat("en", options);
  }
}

/** A stored value of `field`, formatted for display; "—" when absent. */
export function formatValue(value: unknown, field: FieldSpec | undefined, lang: string): string {
  if (value === null || value === undefined || value === "") return NO_VALUE;
  if (typeof value === "number") {
    if (field?.type === "money" && field.currency) {
      return number(lang, { style: "currency", currency: field.currency }).format(value);
    }
    if (field?.type === "percent") {
      return number(lang, { style: "percent", maximumFractionDigits: 1 }).format(value / 100);
    }
    return number(lang, { maximumFractionDigits: 2 }).format(value);
  }
  if (field?.type === "date" && typeof value === "string") return formatDay(value, lang);
  return String(value);
}

/** A relative change (0.2 = +20 %) as a signed percentage. */
export function formatChange(fraction: number, lang: string): string {
  return number(lang, {
    style: "percent",
    maximumFractionDigits: 1,
    signDisplay: "exceptZero",
  }).format(fraction);
}

/** A compact axis tick for a numeric value. */
export function formatCompact(value: number, lang: string): string {
  return number(lang, { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

function dateFormat(lang: string, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  try {
    return new Intl.DateTimeFormat(lang, options);
  } catch {
    return new Intl.DateTimeFormat("en", options);
  }
}

/** A calendar day (`YYYY-MM-DD`, or the date part of an ISO datetime at UTC
 *  midnight). Formatted in UTC so it never slips to the day before. */
export function formatDay(iso: string, lang: string): string {
  const time = Date.parse(iso.length === 10 ? `${iso}T00:00:00Z` : iso);
  if (Number.isNaN(time)) return iso;
  return dateFormat(lang, { dateStyle: "medium", timeZone: "UTC" }).format(time);
}

/** When a value was for: a moment (snapshot) in the reader's time, a month
 *  bucket as month and year, any other period by its first day. */
export function formatAsOf(
  asOf: string,
  grain: Grain,
  lang: string,
  bucket: string = "grain",
): string {
  const time = Date.parse(asOf.length === 10 ? `${asOf}T00:00:00Z` : asOf);
  if (Number.isNaN(time)) return asOf;
  if (bucket === "month" || (bucket === "grain" && grain === "month")) {
    return dateFormat(lang, { month: "short", year: "numeric", timeZone: "UTC" }).format(time);
  }
  if (grain === "moment" && bucket === "grain" && asOf.length > 10) {
    return dateFormat(lang, { dateStyle: "medium", timeStyle: "short" }).format(time);
  }
  return formatDay(asOf, lang);
}

/** Why a block shows no data, translated. A refused query
 *  (`422 board_query:<code>`) never shows its detail. */
export function blockErrorText(error: unknown, t: Translate): string {
  if (error instanceof ApiError) {
    if (error.status === 410)
      return t("This dashboard was deleted", "Dieses Dashboard wurde gelöscht");
    if (error.status === 422)
      return t("This block can't be shown", "Dieser Block kann nicht angezeigt werden");
    if (error.status === 403 || error.status === 404) {
      return t("You have no access to this dashboard", "Kein Zugriff auf dieses Dashboard");
    }
  }
  return t("This block couldn't be loaded", "Dieser Block konnte nicht geladen werden");
}
