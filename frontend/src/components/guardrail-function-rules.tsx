import { useState } from "react";
import { Check, Info, Plus, Trash2, X } from "lucide-react";
import { toast } from "sonner";
import { resolveTranslation, useLang, useT, type Lang } from "@/lib/i18n";
import {
  useInterpretGuardrail,
  useInterpretGuardrailsFromInstruction,
  type GuardrailInterpretation,
} from "@/lib/hooks-agent-detail";
import { useCan } from "@/lib/governance-hooks";
import { cn } from "@/lib/utils";
import type { GuardrailAttribute } from "@/lib/hooks";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import {
  libraryPolicyOf,
  policyOf,
  type Condition,
  type GuardrailLibraryEntry,
  type GuardrailPreset,
  type GuardrailValue,
} from "@/components/guardrail-preset-picker";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

// The one internal policy model -- 4 states, matching `authz/pdp.py`'s own
// vocabulary exactly (`with_limits` is "has an applicable Condition", not a
// field of its own). Both input paths below -- a direct checkbox toggle and
// the free-text "Definition" shortcut -- write into the SAME `GuardrailValue`
// fields (`only`/`approvalActions`/`conditions`) through the SAME `applyMode`/
// `applyConditionsForFunction` writers; there is no second, parallel
// guardrail representation anywhere in this component.
type FunctionMode = "deny" | "allow" | "approval" | "with_limits";
// The subset `applyMode`/`applyGateMode` can express directly (a blanket
// allow/deny/unconditional-approval) -- "with_limits" is never one of these,
// it is always written via `applyConditionsForFunction` instead, since it
// needs a `Condition[]`, not a single mode flag.
type SimpleMode = "deny" | "allow" | "approval";
type Right = "read" | "modify";

// Both base rights are representable with the exact same deny/allow/approval
// vocabulary as a real function -- `authz/pdp.py`'s `authorize_tool_call`
// already treats a bare right name inside `approvalActions` as gating every
// tool that shares it (`RIGHTS = ("read", "modify")`), so "read"/"modify" as
// pseudo-function-names is not a new convention, just the two gates the
// backend already supports, surfaced as one always-present pinned row
// instead of the toggle buttons that used to sit above this table. That old
// pair only ever flipped `value.read`/`value.modify` directly and had no
// per-function counterpart; this one pinned row -- with its own Read AND
// Modify checkbox, like a GitHub fine-grained token's per-resource
// read/write pair -- is the one place left that can grant or revoke a base
// right directly. Gates never carry Conditions: a `GuardrailAttribute`
// declares which real tool names it applies to, never "read"/"modify", so
// "with limits" has no meaning for this row.
const READ_GATE: Right = "read";
const MODIFY_GATE: Right = "modify";
const GATES: Right[] = [READ_GATE, MODIFY_GATE];
// Modify is a superset of read in practice -- there is no real-world action
// that changes data without first being able to see it -- so the two
// checkboxes on the pinned gate row aren't fully independent: granting
// modify always grants read too, and revoking read (the thing modify
// depends on) always revokes modify too. Only this one direction is a real
// constraint; read without modify is a completely normal, common state.
function withGateInvariant(value: GuardrailValue, changed: Right): GuardrailValue {
  if (changed === "modify" && value.modify && !value.read) {
    return applyGateMode(value, "read", "allow");
  }
  if (changed === "read" && !value.read && value.modify) {
    return applyGateMode(value, "modify", "deny");
  }
  return value;
}

function isGate(name: string): name is Right {
  return name === READ_GATE || name === MODIFY_GATE;
}

// Which of a connection's declared `GuardrailAttribute` keys can actually
// apply to one function -- the same "empty tools = every tool" scoping rule
// `agent/engine.py` uses at runtime (`not spec.get("tools") or tc.name in
// spec["tools"]`), replicated here so the UI's notion of "this function has
// limits" never drifts from what enforcement would actually evaluate.
function attributeKeysForTool(
  guardrailAttributes: GuardrailAttribute[],
  name: string,
): Set<string> {
  return new Set(
    guardrailAttributes
      .filter((a) => a.tools.length === 0 || a.tools.includes(name))
      .map((a) => a.key),
  );
}

// `approvalEur` is a connection-wide legacy scalar (see `applyMode`'s own
// comment) that `authorize_tool_call` still evaluates directly today,
// independent of `conditions` -- but nothing ever decomposed it into a
// per-function preview here, so every preset/library entry that expresses
// its restriction as "approval_eur only" (most of odoo_mcp's use-case
// entries: sales/purchasing/inventory autonomous-with-limit, the quote and
// opportunity thresholds) rendered as a plain "allow" for every function it
// touches and vanished from this table entirely. Synthesizing the same
// threshold as a `Condition` against whichever declared numeric
// `GuardrailAttribute` actually applies to this function keeps the display
// truthful to what enforcement already does, without adding a second
// threshold concept -- an explicit `conditions` entry for this function (a
// real, operator-authored one) always wins; this is only a fallback for
// functions that have nothing more specific.
function syntheticApprovalEurCondition(
  value: GuardrailValue,
  name: string,
  guardrailAttributes: GuardrailAttribute[],
): Condition | null {
  if (value.approvalEur == null) return null;
  const attr = guardrailAttributes.find(
    (a) => a.datatype === "number" && (a.tools.length === 0 || a.tools.includes(name)),
  );
  if (!attr) return null;
  return {
    attribute: attr.key,
    datatype: attr.datatype,
    operator: ">=",
    value: value.approvalEur,
    then: "require_approval",
  };
}

function conditionsForFunction(
  value: GuardrailValue,
  name: string,
  guardrailAttributes: GuardrailAttribute[],
): Condition[] {
  const keys = attributeKeysForTool(guardrailAttributes, name);
  const explicit = value.conditions.filter((c) => keys.has(c.attribute));
  if (explicit.length > 0) return explicit;
  const synthetic = syntheticApprovalEurCondition(value, name, guardrailAttributes);
  return synthetic ? [synthetic] : [];
}

function stripConditionsForFunction(
  value: GuardrailValue,
  name: string,
  guardrailAttributes: GuardrailAttribute[],
): Condition[] {
  const keys = attributeKeysForTool(guardrailAttributes, name);
  return value.conditions.filter((c) => !keys.has(c.attribute));
}

// Derived, never stored separately: `only` (a reachability allowlist),
// `approvalActions` (per-call approval gate), `conditions` (per-attribute
// "with limits" rules) and `read`/`modify` (the blanket rights) already
// exist on `GuardrailValue` -- see that interface's own doc comment in
// guardrail-preset-picker.tsx for why `only` must never be dropped. A
// function absent from a non-empty `only` is unreachable at all (both read
// and modify -- `authorize_tool_call` in authz/pdp.py gates on it
// unconditionally, not per right), which is why "deny" wins over
// "approval"/"with_limits" below rather than the three being combinable --
// same precedence order `authorize_tool_call` itself evaluates in
// (deny checks, then unconditional `approval_actions`, then `conditions`).
function modeOf(
  value: GuardrailValue,
  name: string,
  guardrailAttributes: GuardrailAttribute[] = [],
): FunctionMode {
  if (isGate(name)) {
    if (!value[name]) return "deny";
    if (value.approvalActions.includes(name)) return "approval";
    return "allow";
  }
  if (value.only.length > 0 && !value.only.includes(name)) return "deny";
  if (value.approvalActions.includes(name)) return "approval";
  if (conditionsForFunction(value, name, guardrailAttributes).length > 0) return "with_limits";
  return "allow";
}

// Writes exactly the fields a blanket (non-"with_limits") function-level
// rule can actually change. `only` starts empty meaning "every function
// reachable" -- singling one function out by name (deny, or approval while
// other functions stay unrestricted) first has to materialize that implicit
// "everything" into an explicit list, same rule `capabilities.py::
// _apply_guardrail_set` follows backend-side for the exact same reason.
// Also clears any Condition this function carried: switching to a plain
// deny/allow/approval and leaving a stale "with limits" rule behind would
// silently resurrect it the moment the function is switched back, which is
// not what un-checking "with limits" means to an operator.
function applyMode(
  value: GuardrailValue,
  catalog: string[],
  name: string,
  mode: SimpleMode,
  thresholdEur: number | null,
  guardrailAttributes: GuardrailAttribute[],
): GuardrailValue {
  let only = value.only.length > 0 ? [...value.only] : mode === "deny" ? [...catalog] : [];
  let approvalActions = value.approvalActions.filter((a) => a !== name);
  if (mode === "deny") {
    only = only.filter((n) => n !== name);
  } else {
    if (only.length > 0 && !only.includes(name)) only.push(name);
    if (mode === "approval") approvalActions = [...approvalActions, name];
  }
  return {
    ...value,
    only,
    approvalActions,
    // A single connection-wide field today (see FreeGuardrailControls's own
    // €-input) -- there is no per-function threshold to write into, so an
    // approval row that names one sets the same field the manual control
    // does. Never cleared by a row that doesn't mention a threshold at all.
    approvalEur: mode === "approval" && thresholdEur != null ? thresholdEur : value.approvalEur,
    conditions: stripConditionsForFunction(value, name, guardrailAttributes),
  };
}

// The "with limits" writer -- the ONE place, shared by a future manual
// Conditions editor and the free-text shortcut below, that ever writes
// `GuardrailValue.conditions`. Replaces (not appends to) whatever Conditions
// this function already had, same replace-in-place discipline `applyMode`
// uses for `only`/`approvalActions`.
function applyConditionsForFunction(
  value: GuardrailValue,
  name: string,
  guardrailAttributes: GuardrailAttribute[],
  conditions: Condition[],
): GuardrailValue {
  const only = value.only.length > 0 ? [...value.only] : [];
  if (only.length > 0 && !only.includes(name)) only.push(name);
  const approvalActions = value.approvalActions.filter((a) => a !== name);
  const kept = stripConditionsForFunction(value, name, guardrailAttributes);
  return { ...value, only, approvalActions, conditions: [...kept, ...conditions] };
}

// A gate is a base right, not a tool name -- routing it through `applyMode`'s
// `only`/`approvalActions` machinery would either no-op (deny) or silently do
// the wrong thing, since `read`/`modify` live outside `only` entirely. This
// is the one path whose deny/allow writes `value.read`/`value.modify`
// directly.
function applyGateMode(value: GuardrailValue, gate: Right, mode: SimpleMode): GuardrailValue {
  if (mode === "deny") {
    return {
      ...value,
      [gate]: false,
      approvalActions: value.approvalActions.filter((a) => a !== gate),
    };
  }
  if (mode === "approval") {
    return {
      ...value,
      [gate]: true,
      approvalActions: value.approvalActions.includes(gate)
        ? value.approvalActions
        : [...value.approvalActions, gate],
    };
  }
  return {
    ...value,
    [gate]: true,
    approvalActions: value.approvalActions.filter((a) => a !== gate),
  };
}

function revertFunction(
  value: GuardrailValue,
  name: string,
  guardrailAttributes: GuardrailAttribute[],
): GuardrailValue {
  if (isGate(name)) return applyGateMode(value, name, "allow");
  return applyMode(value, [], name, "allow", null, guardrailAttributes);
}

// Raw tool names are technical (`merge_pr`, `post_message`) -- shown with a
// friendlier label, but the raw name stays available as a tooltip (`title`)
// for anyone who needs the exact identifier, e.g. to cross-check a plugin's
// manifest. No dictionary of real labels exists anywhere in the stack (tool
// catalogs are just names, see `ConnectionToolNamesDTO`), so this is a
// mechanical humanization, not a translation.
function humanizeName(name: string): string {
  return name
    .split("_")
    .map((word) =>
      word.length <= 2 ? word.toUpperCase() : word.charAt(0).toUpperCase() + word.slice(1),
    )
    .join(" ");
}

function decisionLabel(
  t: (en: string, de: string) => string,
  decision: FunctionMode | GuardrailInterpretation["decision"],
): string {
  switch (decision) {
    case "deny":
    case "not_allowed":
      return t("Not allowed", "Nicht erlaubt");
    case "approval":
    case "approval_required":
      return t("Approval required", "Freigabe erforderlich");
    case "with_limits":
      return t("With limits", "Mit Grenzen");
    default:
      return t("Self-sufficient", "Selbstständig");
  }
}

// Maps a `GuardrailInterpretation`'s decision onto the same `FunctionMode`
// vocabulary `modeOf` produces from a live `value` -- lets a still-pending
// (not yet accepted) result drive the row's Allowed checkboxes and decision
// label exactly like an already-applied rule would, instead of the row
// looking untouched (still "allow") until the operator accepts it.
function modeFromDecision(decision: GuardrailInterpretation["decision"]): FunctionMode {
  switch (decision) {
    case "not_allowed":
      return "deny";
    case "approval_required":
      return "approval";
    case "with_limits":
      return "with_limits";
    default:
      return "allow";
  }
}

function thenLabel(t: (en: string, de: string) => string, then: Condition["then"]): string {
  if (then === "deny") return t("not allowed", "nicht erlaubt");
  if (then === "allow") return t("allowed", "erlaubt");
  return t("approval required", "Freigabe erforderlich");
}

// Human-readable "IF attribute OPERATOR value THEN then" line -- the
// structured artifact the operator is asked to confirm, not the free text
// that produced it. `label` comes from the connection's own declared
// `GuardrailAttribute` when available, falling back to the raw key so a
// condition still renders even if the catalog was resolved without labels.
function describeCondition(
  c: Condition,
  guardrailAttributes: GuardrailAttribute[],
  t: (en: string, de: string) => string,
): string {
  const attr = guardrailAttributes.find((a) => a.key === c.attribute);
  const label = attr?.label ?? c.attribute;
  const value = Array.isArray(c.value) ? c.value.join(", ") : String(c.value);
  return `${label} ${c.operator} ${value} → ${thenLabel(t, c.then)}`;
}

interface Suggestion {
  // `${entry key}::${name}` -- unique per (predefined guardrail, affected
  // function) pair, since several entries can restrict the same function
  // with different wording and ALL of them must show up as their own row
  // (not deduplicated down to whichever came first).
  id: string;
  name: string;
  mode: FunctionMode;
  thresholdEur: number | null;
  definition: string;
  policy: GuardrailValue;
}

// Decomposes a capa's presets/library (whole-connection policies) into the
// same per-function shape this table already understands: for every entry,
// walk the catalog (plus both base-right gates) through `modeOf` as if that
// single entry's policy were applied, and keep the (function, mode) pairs
// that come out deny/approval/with_limits. This is how a plugin's predefined
// guardrails end up as rows here rather than a second, separate picker --
// see `ToolGuardrailEditorPanel`'s "aus einem Guss" fix.
//
// Deliberately NOT deduplicated across entries: two entries that both deny
// `delete_record` with different explanations are two different predefined
// guardrails and both must appear (design ask: "jede vordefinierte
// Guardrail... soll auch in diesem Pattern sein" -- every one of them, not
// just the first to claim a function). The `listed` filter in the component
// below is what actually removes a suggestion once one of these rows -- or a
// manual one -- is saved for that function.
function suggestionsFrom(
  presets: GuardrailPreset[],
  guardrailLibrary: GuardrailLibraryEntry[] | null,
  toolCatalog: string[],
  guardrailAttributes: GuardrailAttribute[],
  lang: Lang,
): Suggestion[] {
  const entries: Array<{ key: string; policy: GuardrailValue; text: string }> =
    guardrailLibrary && guardrailLibrary.length > 0
      ? guardrailLibrary.map((entry) => ({
          key: entry.key,
          policy: libraryPolicyOf(entry),
          text:
            resolveTranslation(entry.summary, entry.summaryTranslations, lang) ||
            resolveTranslation(entry.label, entry.labelTranslations, lang),
        }))
      : presets.map((preset) => ({
          key: preset.key,
          policy: policyOf(preset),
          text:
            resolveTranslation(preset.summary, preset.summaryTranslations, lang) ||
            resolveTranslation(preset.label, preset.labelTranslations, lang),
        }));

  const names = [...toolCatalog, ...GATES];
  const out: Suggestion[] = [];
  for (const { key, policy, text } of entries) {
    for (const name of names) {
      const mode = modeOf(policy, name, guardrailAttributes);
      if (mode === "allow") continue;
      out.push({
        id: `${key}::${name}`,
        name,
        mode,
        thresholdEur: policy.approvalEur,
        definition: text,
        policy,
      });
    }
  }
  return out;
}

interface Row {
  id: string;
  name: string;
  kind: "gate" | "existing" | "suggestion" | "new";
}

// The mockup's inner table: one pinned row for the base rights (always
// present, its own Read AND Modify checkbox, both live -- this replaces the
// toggle pair that used to sit above this table), one row per function this
// connection exposes that already carries an explicit rule (deny, approval,
// or with limits), one row per tool-provided suggestion not yet accepted,
// plus an "+ Neue Guardrail" affordance for adding one more. Every
// non-suggestion row's checkbox is a direct, immediate control -- like a
// fine-grained access token's permission list, checking/unchecking it calls
// `onChange` right away with a plain allow/deny, no Definition text or
// interpret round-trip required for that.
// The Definition field + ✓ stays the way to express anything with more
// nuance than a flat allow/deny (an approval threshold, a condition in
// prose) -- that's still the only path that calls
// `interpret_guardrail_definition`, and it is a SHORTCUT into the same
// `conditions`/`approvalActions`/`only` fields a manual Conditions editor
// would also write, never a second policy representation. The LLM is called
// once, produces a structured `GuardrailInterpretation`, and that structured
// result -- never the free text, and never the LLM again -- is what the
// operator reviews (a preview row with Accept/Discard) and, only on
// Accept, gets merged into the draft `value` below. A suggestion row's
// checkbox stays a preview (disabled): it isn't part of the draft yet, ✓ is
// what promotes it -- promoting a suggestion applies immediately (it is
// already a vetted structured policy, not free text awaiting review).
// This is deliberately not a second write path: the outer panel's own Save
// button is still the only thing that ever calls `PUT /agents/{id}/narrowing`
// -- every control in this table only updates the draft `value` held in
// memory.
export function GuardrailFunctionRules({
  agentId,
  connectionName,
  toolCatalog,
  readTools = [],
  guardrailAttributes = [],
  value,
  onChange,
  presets = [],
  guardrailLibrary = null,
}: {
  agentId: string;
  connectionName: string;
  toolCatalog: string[];
  /** Which of `toolCatalog`'s names are classified `read` (the rest are
   * `modify`) -- mirrors `authz.pdp.required_right`'s own fail-closed rule
   * (unlisted = modify), so a name absent from both is still shown as a
   * Modify row here, matching what the backend would actually enforce. */
  readTools?: string[];
  /** The connection's declared `GuardrailAttribute`s (`McpConnection.
   * guardrailAttributes`) -- the closed catalog "with limits" Conditions may
   * reference for this connection. Empty for a connection whose plugin
   * declares none, in which case no function here can ever show
   * "with limits" and the free-text shortcut can only resolve to one of the
   * other 3 states. */
  guardrailAttributes?: GuardrailAttribute[];
  value: GuardrailValue;
  onChange: (next: GuardrailValue) => void;
  presets?: GuardrailPreset[];
  guardrailLibrary?: GuardrailLibraryEntry[] | null;
}) {
  const t = useT();
  const { lang } = useLang();
  const can = useCan();
  const mayUseCopilot = can("copilot:use");
  const interpret = useInterpretGuardrail(agentId);
  const interpretFromInstruction = useInterpretGuardrailsFromInstruction(agentId);
  const [suggesting, setSuggesting] = useState(false);
  const [definitions, setDefinitions] = useState<Record<string, string>>({});
  const [newRows, setNewRows] = useState<string[]>([]);
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const [savingId, setSavingId] = useState<string | null>(null);
  const [rightFilter, setRightFilter] = useState<Right | null>(null);
  // The last structured result the interpreter produced for a row, awaiting
  // the operator's explicit Accept -- see the component doc comment above.
  // Never applied to `value` on its own; only `acceptPending` does that.
  const [pending, setPending] = useState<
    Record<string, { targetName: string; result: GuardrailInterpretation }>
  >({});
  // Rows the user has directly interacted with this session, kept visible
  // even once their live mode becomes "allow" again -- without this, ticking
  // a restricted row's checkbox back to allowed made it vanish mid-click
  // (it fell out of `ruled` on the very next render). Only cleared by the
  // row's own Remove button, never by the toggle that put it here.
  const [manuallyShown, setManuallyShown] = useState<Set<string>>(new Set());

  const rightOf = (name: string): Right =>
    isGate(name) ? name : readTools.includes(name) ? "read" : "modify";

  const ruled = toolCatalog.filter(
    (name) => modeOf(value, name, guardrailAttributes) !== "allow" || manuallyShown.has(name),
  );
  const listed = new Set(ruled);
  const gateAlreadySet = new Set(
    GATES.filter((g) => modeOf(value, g, guardrailAttributes) !== "allow"),
  );
  const suggestions = suggestionsFrom(
    presets,
    guardrailLibrary,
    toolCatalog,
    guardrailAttributes,
    lang,
  ).filter(
    (s) =>
      !listed.has(s.name) &&
      !(isGate(s.name) && gateAlreadySet.has(s.name)) &&
      !newRows.includes(s.name) &&
      !dismissed.has(s.id),
  );
  const suggestionById = new Map(suggestions.map((s) => [s.id, s]));
  const suggestedNames = new Set(suggestions.map((s) => s.name));

  // The one pinned gate row is always present, independent of `rightFilter`
  // -- it IS the base rights, not something a right filter should ever hide.
  // A predefined guardrail whose ENTIRE restriction is one of the two gates
  // (e.g. every `*_read_only.toml` entry) has nowhere else to live: it folds
  // its definition text into this row as a prefill (via `suggestionFor`
  // below) rather than spawning a second row for a right this same row
  // already covers.
  const gateRows: Row[] = [{ id: "gate", name: "gate", kind: "gate" as const }];
  const functionRows: Row[] = [
    ...ruled.map((name) => ({ id: name, name, kind: "existing" as const })),
    ...suggestions
      .filter((s) => !isGate(s.name))
      .map((s) => ({ id: s.id, name: s.name, kind: "suggestion" as const })),
    ...newRows
      .filter((name) => !listed.has(name))
      .map((name) => ({ id: name, name, kind: "new" as const })),
  ].filter((row) => rightFilter === null || rightOf(row.name) === rightFilter);
  const rows: Row[] = [...gateRows, ...functionRows];

  const pickable = toolCatalog.filter(
    (name) => !listed.has(name) && !newRows.includes(name) && !suggestedNames.has(name),
  );

  // A gate-targeting suggestion is keyed by whichever single gate it
  // actually restricts (`suggestionsFrom` walks both independently) -- the
  // merged row shows whichever one exists, preferring modify since granting
  // it also grants read (see `withGateInvariant`), so it's the more
  // encompassing of the two to surface first.
  function suggestionFor(row: Row): Suggestion | undefined {
    if (row.kind === "gate") {
      return (
        suggestions.find((s) => s.name === MODIFY_GATE) ??
        suggestions.find((s) => s.name === READ_GATE)
      );
    }
    return suggestionById.get(row.id);
  }

  function toggle(row: Row, nextChecked: boolean) {
    const nextMode: SimpleMode = nextChecked ? "allow" : "deny";
    const next = isGate(row.name)
      ? applyGateMode(value, row.name, nextMode)
      : applyMode(value, toolCatalog, row.name, nextMode, null, guardrailAttributes);
    onChange(next);
    if (!isGate(row.name)) setManuallyShown((prev) => new Set(prev).add(row.name));
    if (row.kind === "new") setNewRows((prev) => prev.filter((n) => n !== row.id));
  }

  // The pinned gate row's two checkboxes, unlike every other row, each write
  // a different field of the SAME row -- so they need their own handler
  // rather than `toggle`'s one-checkbox-per-row assumption, plus the
  // modify-implies-read invariant `toggle` never has to think about.
  function toggleGateRight(right: Right, nextChecked: boolean) {
    const next = applyGateMode(value, right, nextChecked ? "allow" : "deny");
    onChange(withGateInvariant(next, right));
  }

  function clearRowDraftState(rowId: string) {
    setNewRows((prev) => prev.filter((n) => n !== rowId));
    setDefinitions((prev) => {
      const next = { ...prev };
      delete next[rowId];
      return next;
    });
    setPending((prev) => {
      const next = { ...prev };
      delete next[rowId];
      return next;
    });
  }

  async function save(row: Row) {
    const suggestion = suggestionFor(row);
    const definition = (definitions[row.id] ?? suggestion?.definition ?? "").trim();
    if (!definition) return;
    // For the merged gate row, a suggestion pins down which gate the text is
    // actually about; free-typed text with no suggestion defaults to modify,
    // the more encompassing of the two (see `suggestionFor`'s own comment).
    const targetName = row.kind === "gate" ? (suggestion?.name ?? MODIFY_GATE) : row.name;
    // Promoting an untouched suggestion is applying an already-structured,
    // pre-vetted policy -- not free text -- so it takes effect immediately,
    // same as a direct checkbox toggle. Only a typed/edited definition goes
    // through the interpreter, and even then only as far as a preview: see
    // `acceptPending` for the step that actually merges it into `value`.
    if (suggestion && definitions[row.id] === undefined) {
      let next: GuardrailValue;
      if (suggestion.mode === "with_limits") {
        // A gate never carries Conditions (see `applyMode`'s own gate
        // comment) -- a with-limits suggestion is always a real function, so
        // `targetName` here is never a gate name.
        next = applyConditionsForFunction(
          value,
          targetName,
          guardrailAttributes,
          conditionsForFunction(suggestion.policy, targetName, guardrailAttributes),
        );
      } else {
        next = isGate(targetName)
          ? applyGateMode(value, targetName, suggestion.mode)
          : applyMode(
              value,
              toolCatalog,
              targetName,
              suggestion.mode,
              suggestion.thresholdEur,
              guardrailAttributes,
            );
        if (isGate(targetName)) next = withGateInvariant(next, targetName);
      }
      onChange(next);
      if (!isGate(targetName)) setManuallyShown((prev) => new Set(prev).add(targetName));
      clearRowDraftState(row.id);
      return;
    }
    setSavingId(row.id);
    try {
      const result = await interpret.mutateAsync({
        connectionName,
        function: targetName,
        definition,
      });
      setPending((prev) => ({ ...prev, [row.id]: { targetName, result } }));
    } catch {
      toast.error(
        t(
          "Couldn't understand that guardrail — try phrasing it differently.",
          "Diese Guardrail konnte nicht verstanden werden — versuche es anders zu formulieren.",
        ),
      );
    } finally {
      setSavingId(null);
    }
  }

  // The only place a free-text interpretation ever reaches the draft
  // `value` -- an explicit operator action on the structured preview
  // `save` produced, never automatic. Writes through the exact same
  // `applyMode`/`applyConditionsForFunction` a manual edit would.
  function acceptPending(row: Row) {
    const entry = pending[row.id];
    if (!entry) return;
    const { targetName, result } = entry;
    let next: GuardrailValue;
    if (result.decision === "with_limits") {
      next = applyConditionsForFunction(value, targetName, guardrailAttributes, result.conditions);
    } else {
      const mode: SimpleMode =
        result.decision === "not_allowed"
          ? "deny"
          : result.decision === "approval_required"
            ? "approval"
            : "allow";
      next = isGate(targetName)
        ? applyGateMode(value, targetName, mode)
        : applyMode(value, toolCatalog, targetName, mode, null, guardrailAttributes);
      if (isGate(targetName)) next = withGateInvariant(next, targetName);
    }
    onChange(next);
    if (!isGate(targetName)) setManuallyShown((prev) => new Set(prev).add(targetName));
    clearRowDraftState(row.id);
  }

  function discardPending(row: Row) {
    setPending((prev) => {
      const next = { ...prev };
      delete next[row.id];
      return next;
    });
  }

  function remove(row: Row) {
    if (row.kind === "suggestion") {
      setDismissed((prev) => new Set(prev).add(row.id));
      return;
    }
    const next = revertFunction(value, row.name, guardrailAttributes);
    onChange(next);
    setManuallyShown((prev) => {
      const next = new Set(prev);
      next.delete(row.name);
      return next;
    });
    clearRowDraftState(row.id);
  }

  // "Copilot" button: reads this agent's own instructions (server-side --
  // nothing about them is sent from here) and proposes rules for every
  // function the instructions imply should be restricted, in one call.
  // Lands each proposal as a `pending` preview on a (possibly brand-new)
  // row, exactly like the free-text Definition shortcut's own preview --
  // the operator still has to Accept or Discard every single one; nothing
  // here ever touches `value` directly.
  async function runCopilotSuggestions() {
    setSuggesting(true);
    try {
      const { results } = await interpretFromInstruction.mutateAsync({ connectionName });
      if (results.length === 0) {
        toast.info(
          t(
            "The copilot found nothing in the instructions to restrict here.",
            "Der Copilot hat in der Instruction nichts gefunden, das hier eingeschränkt werden sollte.",
          ),
        );
        return;
      }
      setNewRows((prev) => {
        const additions = results
          .map((r) => r.function)
          .filter((name) => !listed.has(name) && !prev.includes(name));
        return [...prev, ...additions];
      });
      setPending((prev) => {
        const next = { ...prev };
        for (const r of results) {
          next[r.function] = {
            targetName: r.function,
            result: { decision: r.decision, conditions: r.conditions },
          };
        }
        return next;
      });
    } catch {
      toast.error(t("Couldn't reach the copilot", "Copilot war nicht erreichbar"));
    } finally {
      setSuggesting(false);
    }
  }

  return (
    <div className="mt-4 overflow-hidden rounded-md border border-border/70">
      <div className="flex flex-wrap items-center gap-2 border-b border-border/70 bg-background/30 p-2.5">
        {mayUseCopilot && (
          <button
            type="button"
            onClick={runCopilotSuggestions}
            disabled={suggesting}
            className="flex items-center gap-1.5 rounded-full border border-primary/60 bg-primary/10 px-3 py-1 text-xs text-primary transition hover:bg-primary/20 disabled:opacity-60"
            title={t(
              "Let the copilot read this agent's instructions and suggest guardrails",
              "Den Copilot die Instruction dieses Agenten lesen und Guardrails vorschlagen lassen",
            )}
          >
            <img src="/octopus_oc8.svg" alt="" className="h-3.5 w-3.5" draggable={false} />
            {suggesting
              ? t("Thinking…", "Denkt nach…")
              : t("Suggest from instructions", "Aus Instruction vorschlagen")}
          </button>
        )}
        <span className="self-center micro-label">{t("Filter", "Filter")}</span>
        {(["read", "modify"] as const).map((right) => (
          <button
            key={right}
            type="button"
            onClick={() => setRightFilter((prev) => (prev === right ? null : right))}
            className={cn(
              "rounded-full border px-3 py-1 text-xs transition",
              rightFilter === right
                ? "border-primary bg-primary/10 text-primary"
                : "border-border text-muted-foreground hover:bg-accent",
            )}
          >
            {right === "read" ? t("Read", "Lesen") : t("Modify", "Verändern")}
          </button>
        ))}
      </div>
      <Table className="text-left text-sm">
        <TableHeader>
          <TableRow className="border-b border-border/70 bg-background/30 micro-label">
            <TableHead className="p-2.5 micro-label">{t("Allowed", "Erlaubt")}</TableHead>
            <TableHead className="p-2.5 micro-label">{t("Function", "Funktion")}</TableHead>
            <TableHead className="w-2/5 p-2.5 micro-label">
              {t("Definition", "Definition")}
            </TableHead>
            <TableHead className="p-2.5 micro-label" />
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row) => {
            const suggestion = suggestionFor(row);
            const isGateRow = row.kind === "gate";
            const pendingResult = pending[row.id];
            // A pending (not yet accepted) interpretation previews as if it
            // were already live -- same idea as a suggestion row's `mode`
            // coming from `suggestion.mode` rather than `modeOf(value, ...)`
            // -- so the row reads as one complete, filled-in proposal
            // (Allowed state + decision label) instead of looking untouched
            // until Accept is clicked.
            const mode =
              row.kind === "suggestion" && suggestion
                ? suggestion.mode
                : pendingResult
                  ? modeFromDecision(pendingResult.result.decision)
                  : modeOf(value, row.name, guardrailAttributes);
            const right = rightOf(row.name);
            const checked = mode !== "deny";
            const isSuggestion = row.kind === "suggestion";
            const isPending = Boolean(pendingResult);
            const rowConditions =
              !isGateRow && mode === "with_limits"
                ? pendingResult
                  ? pendingResult.result.conditions
                  : conditionsForFunction(value, row.name, guardrailAttributes)
                : [];
            return (
              <TableRow key={row.id} className="border-b border-border/60 last:border-0">
                <TableCell className="p-2.5 align-top">
                  {isGateRow ? (
                    <div className="flex flex-col gap-1">
                      {GATES.map((gate) => (
                        <label
                          key={gate}
                          className="flex cursor-pointer items-center gap-1.5 text-xs text-muted-foreground"
                        >
                          <input
                            type="checkbox"
                            checked={modeOf(value, gate, guardrailAttributes) !== "deny"}
                            onChange={(e) => toggleGateRight(gate, e.target.checked)}
                          />
                          {gate === "read" ? t("Read", "Lesen") : t("Modify", "Verändern")}
                        </label>
                      ))}
                    </div>
                  ) : (
                    <div className="flex flex-col gap-1">
                      {GATES.map((gate) => {
                        const isActive = gate === right;
                        // A function is strictly read XOR modify (its
                        // `rightOf` classification never changes), so the
                        // other checkbox isn't a live control here -- it
                        // just states the implication: modify always
                        // includes read (forced on), and a read-only
                        // function has no modify capability at all (forced
                        // off). Same "GitHub fine-grained token" shape as
                        // the pinned gate row, minus the second live field.
                        const isChecked = isActive ? checked : gate === "read";
                        // A plain `title` on the checkbox itself is
                        // unreliable here: several browsers don't fire hover
                        // events on a `disabled` form control at all, so the
                        // explanation would silently never show. The (i)
                        // icon is never disabled, so its tooltip always
                        // works, and it visibly signals "hover me" where a
                        // disabled checkbox alone doesn't.
                        const impliedReason = !isActive
                          ? gate === "read"
                            ? t(
                                "Included automatically: modifying requires read access.",
                                "Automatisch inbegriffen: Verändern setzt Lesezugriff voraus.",
                              )
                            : t(
                                "This function is read-only in the plugin's manifest; it has no modify action.",
                                "Diese Funktion ist laut Plugin-Manifest nur lesend; es gibt keine Verändern-Aktion dafür.",
                              )
                          : undefined;
                        return (
                          <label
                            key={gate}
                            className={cn(
                              "flex items-center gap-1.5 text-xs text-muted-foreground",
                              isActive && !isSuggestion && !isPending && "cursor-pointer",
                            )}
                            title={
                              impliedReason ??
                              (isSuggestion || isPending
                                ? t(
                                    "Accept this suggestion to make it a live rule.",
                                    "Übernimm diesen Vorschlag, damit er wirksam wird.",
                                  )
                                : undefined)
                            }
                          >
                            <input
                              type="checkbox"
                              checked={isChecked}
                              disabled={!isActive || isSuggestion || isPending}
                              onChange={isActive ? (e) => toggle(row, e.target.checked) : undefined}
                            />
                            {gate === "read" ? t("Read", "Lesen") : t("Modify", "Verändern")}
                            {impliedReason && (
                              <TooltipProvider delayDuration={150}>
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    <Info className="h-3 w-3 shrink-0 text-muted-foreground/70" />
                                  </TooltipTrigger>
                                  <TooltipContent>{impliedReason}</TooltipContent>
                                </Tooltip>
                              </TooltipProvider>
                            )}
                          </label>
                        );
                      })}
                    </div>
                  )}
                </TableCell>
                <TableCell className="p-2.5 align-top">
                  {isGateRow ? (
                    <span className="font-medium">
                      {t("All actions (base rights)", "Alle Aktionen (Basisrechte)")}
                    </span>
                  ) : row.kind === "new" && !pendingResult ? (
                    <select
                      value={row.name}
                      onChange={(e) => {
                        const next = e.target.value;
                        setNewRows((prev) => prev.map((n) => (n === row.name ? next : n)));
                        setDefinitions((prev) => {
                          const { [row.name]: moved, ...rest } = prev;
                          return moved !== undefined ? { ...rest, [next]: moved } : rest;
                        });
                      }}
                      className="rounded-md border border-border bg-background/40 px-2 py-1 text-xs text-foreground"
                    >
                      <option value={row.name}>{humanizeName(row.name)}</option>
                      {pickable.map((name) => (
                        <option key={name} value={name}>
                          {humanizeName(name)}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <span className="font-medium" title={row.name}>
                      {humanizeName(row.name)}
                    </span>
                  )}
                  {!isGateRow && mode !== "allow" && (
                    <div className="mt-0.5 micro-label">{decisionLabel(t, mode)}</div>
                  )}
                </TableCell>
                <TableCell className="p-2.5 align-top">
                  <input
                    type="text"
                    value={
                      definitions[row.id] ??
                      suggestion?.definition ??
                      (pendingResult ? decisionLabel(t, pendingResult.result.decision) : "")
                    }
                    onChange={(e) =>
                      setDefinitions((prev) => ({ ...prev, [row.id]: e.target.value }))
                    }
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        void save(row);
                      }
                    }}
                    readOnly={isPending}
                    placeholder={t(
                      "optional, e.g. up to 100 units on its own, above that approval",
                      "optional, z. B. bis 100 Stück selbstständig, darüber Freigabe",
                    )}
                    className="w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm text-foreground outline-none focus:border-primary/50 read-only:opacity-70"
                  />
                  {rowConditions.length > 0 && (
                    <ul className="mt-1.5 space-y-0.5 text-xs text-muted-foreground">
                      {rowConditions.map((c, i) => (
                        <li key={i}>{describeCondition(c, guardrailAttributes, t)}</li>
                      ))}
                    </ul>
                  )}
                </TableCell>
                <TableCell className="p-2.5 text-right align-top">
                  <div className="flex justify-end gap-1">
                    {pendingResult ? (
                      <>
                        <button
                          type="button"
                          onClick={() => acceptPending(row)}
                          className="inline-flex items-center gap-1 rounded-md border border-primary bg-primary/10 px-2 py-1 text-xs font-medium text-primary transition hover:bg-primary/20"
                        >
                          <Check className="h-3 w-3" />
                          {t("Accept", "Übernehmen")}
                        </button>
                        <button
                          type="button"
                          onClick={() => discardPending(row)}
                          className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition hover:bg-accent hover:text-foreground"
                        >
                          <X className="h-3 w-3" />
                          {t("Discard", "Verwerfen")}
                        </button>
                      </>
                    ) : (
                      <button
                        type="button"
                        aria-label={t("Apply", "Übernehmen")}
                        title={t(
                          "Apply this rule to the draft below (doesn't save yet)",
                          "Diese Regel in den Entwurf unten übernehmen (speichert noch nicht)",
                        )}
                        onClick={() => void save(row)}
                        disabled={
                          savingId === row.id ||
                          !(definitions[row.id] ?? suggestion?.definition ?? "").trim()
                        }
                        className="rounded-md border border-border p-1.5 text-muted-foreground transition hover:bg-accent hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        <Check className="h-3.5 w-3.5" />
                      </button>
                    )}
                    {row.kind !== "gate" && (
                      <button
                        type="button"
                        aria-label={t("Remove", "Entfernen")}
                        onClick={() => remove(row)}
                        className="rounded-md border border-border p-1.5 text-muted-foreground transition hover:bg-accent hover:text-foreground"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    )}
                  </div>
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
      <div className="flex flex-col gap-1.5 border-t border-border/70 p-2">
        <button
          type="button"
          onClick={() => {
            const next = pickable[0];
            if (!next) return;
            setNewRows((prev) => [...prev, next]);
          }}
          disabled={pickable.length === 0}
          className="inline-flex items-center gap-1.5 self-start rounded-md border border-border bg-background/40 px-2.5 py-1.5 text-xs font-medium text-foreground transition hover:bg-background/70 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Plus className="h-3.5 w-3.5" /> {t("New guardrail", "Neue Guardrail")}
        </button>
        <p className="text-xs text-muted-foreground">
          {t(
            "Check a box to allow/deny it right away; ✓ interprets a Definition text into a structured rule you then Accept or Discard. Either way, use the panel's Save button to store your changes.",
            "Häkchen setzen erlaubt/verbietet sofort; ✓ übersetzt einen Definitionstext in eine strukturierte Regel, die du übernimmst oder verwirfst. So oder so speichert erst der Speichern-Button des Panels.",
          )}
        </p>
      </div>
    </div>
  );
}
