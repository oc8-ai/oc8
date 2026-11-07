import type { Condition, GuardrailValue } from "@/components/guardrail-preset-picker";

// The four plain guardrail levels (D3a, design review D2) -- the frontend
// mirror of `backend/src/oc8/authz/levels.py`. Display and input only: a
// level maps onto the existing `GuardrailValue` fields and is never stored.
// Everything a level cannot express (a € threshold, single actions,
// conditions, `only`) is a "limit", shown as a quiet hint and edited under
// "More". `src/test/guardrail-levels.test.ts` carries the same truth table as
// `backend/tests/authz/test_levels.py`, so the two mappings cannot drift.

export type Level = "off" | "read_only" | "ask_first" | "allowed";

/** Strictest first -- the index is the order the picker lists them in, and
 * "looser" means a higher index. */
export const LEVELS: readonly Level[] = ["off", "read_only", "ask_first", "allowed"];

/** The fields a level reads. Loose on purpose: `enabled` only exists on the
 * member/term entries, and `conditions` is optional on older call sites. */
type LevelInput = Pick<GuardrailValue, "read" | "modify" | "approvalActions"> & {
  enabled?: boolean;
  approvalEur?: number | null;
  only?: string[];
  conditions?: Condition[];
};

type Translate = (en: string, de: string) => string;

const ASK_ACTION = "modify";

export function levelOf(v: LevelInput): Level {
  if (v.enabled === false || !(v.read || v.modify)) return "off";
  if (!v.modify) return "read_only";
  if (v.approvalActions.includes(ASK_ACTION)) return "ask_first";
  return "allowed";
}

export function hasLimits(v: LevelInput): boolean {
  return (
    // Write without read: the levels assume a modifying level can read too,
    // so picking one would silently add read -- flag it before that.
    (v.modify && !v.read) ||
    v.approvalEur != null ||
    v.approvalActions.some((a) => a !== ASK_ACTION) ||
    (v.conditions?.length ?? 0) > 0 ||
    (v.only?.length ?? 0) > 0
  );
}

/** Levels looser than what `ceiling` already allows -- the ones a picker
 * greys out. `null` (no ceiling above this level) rules nothing out. */
export function disabledLevelsFor(ceiling: LevelInput | null): Level[] {
  if (!ceiling) return [];
  const max = LEVELS.indexOf(levelOf(ceiling));
  return LEVELS.filter((_, i) => i > max);
}

const FULL: GuardrailValue = {
  read: true,
  modify: true,
  approvalActions: [],
  approvalEur: null,
  only: [],
  conditions: [],
};

/** The value to save for a picked level. Starts from `current` (so the
 * details under "More" survive a level change), else from the ceiling, and
 * only ever removes rights or adds `"modify"` to `approvalActions` relative
 * to the ceiling -- it never loosens past it. */
export function valueForLevel(
  level: Level,
  ceiling: GuardrailValue | null,
  current?: GuardrailValue,
): GuardrailValue {
  const start = current ?? ceiling ?? FULL;
  const capRead = ceiling ? ceiling.read : true;
  const capModify = ceiling ? ceiling.modify : true;
  const without = start.approvalActions.filter((a) => a !== ASK_ACTION);
  // Approval actions the ceiling demands always stay -- including its own
  // "modify" when the ceiling itself is "Needs approval".
  const kept = ceiling ? Array.from(new Set([...without, ...ceiling.approvalActions])) : without;
  const next: GuardrailValue = { ...start, conditions: start.conditions ?? [] };
  if (level === "off") return { ...next, read: false, modify: false };
  if (level === "read_only") {
    return { ...next, read: capRead, modify: false, approvalActions: kept };
  }
  if (level === "ask_first") {
    return {
      ...next,
      read: capRead,
      modify: capModify,
      approvalActions: Array.from(new Set([...kept, ASK_ACTION])),
    };
  }
  return { ...next, read: capRead, modify: capModify, approvalActions: kept };
}

/** `member` scope reads "Ask me first"; department and agent "Needs approval". */
export function levelLabel(level: Level, scope: "member" | "other", t: Translate): string {
  switch (level) {
    case "off":
      return t("Off", "Aus");
    case "read_only":
      return t("Read only", "Nur lesen");
    case "ask_first":
      return scope === "member"
        ? t("Ask me first", "Erst mich fragen")
        : t("Needs approval", "Freigabe nötig");
    case "allowed":
      return t("Allowed", "Erlaubt");
  }
}

/** One sentence explaining a level, shown under it in the picker. */
export function levelHint(level: Level, t: Translate, scope: "member" | "other" = "other"): string {
  switch (level) {
    case "off":
      return t("The agent can't use this tool.", "Der Agent kann dieses Tool nicht nutzen.");
    case "read_only":
      return t(
        "The agent can look things up but change nothing.",
        "Der Agent kann nachsehen, aber nichts verändern.",
      );
    case "ask_first":
      return scope === "member"
        ? t(
            "The agent asks you before it changes anything.",
            "Der Agent fragt dich, bevor er etwas verändert.",
          )
        : t(
            "Every change waits for an approval first.",
            "Jede Änderung wartet erst auf eine Freigabe.",
          );
    case "allowed":
      return t(
        "The agent can read and change things on its own.",
        "Der Agent kann selbstständig lesen und verändern.",
      );
  }
}
