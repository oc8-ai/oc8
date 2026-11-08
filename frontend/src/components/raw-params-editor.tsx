import { Plus, Trash2 } from "lucide-react";
import { useT } from "@/lib/i18n";

// Free-form "Advanced/Raw parameters" -- an operator-typed key/value list
// forwarded verbatim to the provider after every named field (temperature/
// maxTokens/effort), for providers that accept knobs oc8 has no field for
// (OpenRouter's `provider`/`top_p`, ...). Shared between the Models settings
// form (model-level default) and AssignedModelPanel (per-agent override) so
// both surfaces parse/render the same way.

export interface RawParamPair {
  key: string;
  value: string;
}

/** `ModelDTO.extra`/`AgentDetail.extra` -> editable rows. Object/array/number/
 * boolean values round-trip through JSON so a value like `{"order":["a"]}`
 * survives re-editing; a plain string is shown bare (no surrounding quotes). */
export function extraToPairs(extra: Record<string, unknown> | null | undefined): RawParamPair[] {
  if (!extra) return [];
  return Object.entries(extra).map(([key, v]) => ({
    key,
    value: typeof v === "string" ? v : JSON.stringify(v),
  }));
}

/** Rows -> a plain object, or undefined if nothing usable was entered (blank
 * keys/values are dropped rather than sent as empty-string parameters). Each
 * value is parsed as JSON first so numbers/booleans/objects/arrays work,
 * falling back to the raw string when it isn't valid JSON. */
export function pairsToExtra(pairs: RawParamPair[]): Record<string, unknown> | undefined {
  const result: Record<string, unknown> = {};
  for (const { key, value } of pairs) {
    const k = key.trim();
    const v = value.trim();
    if (!k || !v) continue;
    try {
      result[k] = JSON.parse(v);
    } catch {
      result[k] = v;
    }
  }
  return Object.keys(result).length > 0 ? result : undefined;
}

export function RawParamsEditor({
  pairs,
  onChange,
  disabled,
}: {
  pairs: RawParamPair[];
  onChange: (pairs: RawParamPair[]) => void;
  disabled?: boolean;
}) {
  const t = useT();

  const update = (index: number, patch: Partial<RawParamPair>) => {
    onChange(pairs.map((p, i) => (i === index ? { ...p, ...patch } : p)));
  };
  const remove = (index: number) => {
    onChange(pairs.filter((_, i) => i !== index));
  };
  const add = () => {
    onChange([...pairs, { key: "", value: "" }]);
  };

  return (
    <div>
      <span className="mb-1 block micro-label">
        {t("Advanced/Raw parameters (optional)", "Erweitert/Raw-Parameter (optional)")}
      </span>
      <p className="mb-2 text-xs text-muted-foreground">
        {t(
          "Forwarded verbatim to the provider after every named field above -- for knobs oc8 has no dedicated field for (e.g. OpenRouter's provider/top_p). Not validated here; the provider decides what it accepts.",
          "Wird nach allen benannten Feldern oben unverändert an den Anbieter weitergereicht -- für Einstellungen, für die oc8 kein eigenes Feld hat (z. B. OpenRouters provider/top_p). Wird hier nicht validiert -- der Anbieter entscheidet, was er akzeptiert.",
        )}
      </p>
      <div className="space-y-2">
        {pairs.map((pair, i) => (
          <div key={i} className="flex items-center gap-2">
            <input
              type="text"
              placeholder={t("key", "Schlüssel")}
              value={pair.key}
              disabled={disabled}
              onChange={(e) => update(i, { key: e.target.value })}
              className="w-2/5 rounded-md border border-border bg-background/40 px-3 py-2 font-mono text-xs outline-none focus:border-primary/50 disabled:cursor-not-allowed disabled:opacity-60"
            />
            <input
              type="text"
              placeholder={t("value", "Wert")}
              value={pair.value}
              disabled={disabled}
              onChange={(e) => update(i, { value: e.target.value })}
              className="flex-1 rounded-md border border-border bg-background/40 px-3 py-2 font-mono text-xs outline-none focus:border-primary/50 disabled:cursor-not-allowed disabled:opacity-60"
            />
            <button
              type="button"
              aria-label={t("Remove parameter", "Parameter entfernen")}
              disabled={disabled}
              onClick={() => remove(i)}
              className="rounded-md border border-border p-2 text-muted-foreground transition hover:text-destructive disabled:cursor-not-allowed disabled:opacity-60"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </div>
        ))}
      </div>
      <button
        type="button"
        disabled={disabled}
        onClick={add}
        className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium text-foreground transition hover:bg-accent disabled:cursor-not-allowed disabled:opacity-60"
      >
        <Plus className="h-3.5 w-3.5" />
        {t("Add parameter", "Parameter hinzufügen")}
      </button>
    </div>
  );
}
