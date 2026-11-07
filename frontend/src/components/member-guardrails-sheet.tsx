import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { CornerDownLeft, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import type { GuardrailValue } from "@/components/guardrail-preset-picker";
import {
  ToolGuardrailTable,
  describeGuardrailSaveError,
  type ToolGuardrailRow,
} from "@/components/tool-guardrail-table";
import { useMediaQuery } from "@/hooks/use-media-query";
import { hasLimits, levelLabel, levelOf, type Level } from "@/lib/guardrail-levels";
import type { ToolPolicy } from "@/lib/hooks";
import {
  ownRuleCount,
  useMemberGuardrails,
  useMyGuardrails,
  useResetMyGuardrail,
  useSetMyGuardrail,
  type MyToolGuardrail,
} from "@/lib/hooks-member-guardrails";
import { useT } from "@/lib/i18n";
import {
  effectSentence,
  instructionExamples,
  readInstruction,
  summarySentences,
  toolLabel,
  type InstructionReading,
} from "@/lib/member-guardrail-text";
import { cn } from "@/lib/utils";

// "Your guardrails" (D3a, design review D3/D6): where a member sees what an
// agent may do in their name and tightens it -- from the chat header, the
// Copilot's "Guardrails" tab, and "For me" on the agent page. One body
// (`MemberGuardrailsPanel`) in all three: the summary sentences, the
// "never … without you" field, then the shared table at `level="member"`.

function toValue(policy: ToolPolicy): GuardrailValue {
  return {
    read: policy.read,
    modify: policy.modify,
    approvalActions: policy.approvalActions ?? [],
    approvalEur: policy.approvalEur ?? null,
    only: policy.only ?? [],
    conditions: policy.conditions ?? [],
  };
}

/** A "More" edit as the stored member entry (snake_case, like the agent's
 * narrowing save). The server refuses anything looser than department ∩ agent. */
function toEntry(value: GuardrailValue): Record<string, unknown> {
  return {
    enabled: value.read || value.modify,
    read: value.read,
    modify: value.modify,
    approval_actions: value.approvalActions,
    approval_eur: value.approvalEur ?? null,
    ...(value.only && value.only.length > 0 ? { only: value.only } : {}),
    conditions: value.conditions ?? [],
  };
}

function rowSource(source: string): ToolGuardrailRow["source"] {
  return source === "member" || source === "agent" || source === "capa_default"
    ? source
    : "department";
}

function SkeletonRows() {
  return (
    <div className="space-y-2" aria-hidden>
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          data-testid="guardrail-skeleton"
          className="flex items-center justify-between gap-4 rounded-md border border-border p-3"
        >
          <div className="h-4 w-24 rounded bg-muted motion-safe:animate-pulse" />
          <div className="h-4 w-20 rounded bg-muted motion-safe:animate-pulse" />
        </div>
      ))}
    </div>
  );
}

export function MemberGuardrailsPanel({ agentId }: { agentId: string }) {
  const t = useT();
  const fieldId = useId();
  const fieldRef = useRef<HTMLInputElement>(null);
  const { data, isLoading } = useMyGuardrails(agentId);
  const save = useSetMyGuardrail(agentId);
  const reset = useResetMyGuardrail(agentId);
  const [text, setText] = useState("");
  const [reading, setReading] = useState<InstructionReading[] | null>(null);
  const [readingError, setReadingError] = useState<string | null>(null);
  const [rowError, setRowError] = useState<Record<string, string>>({});
  const tools = data?.tools ?? [];
  const empty = !!data && ownRuleCount(data) === 0;

  useEffect(() => {
    if (empty) fieldRef.current?.focus();
  }, [empty]);

  function onRead(event: FormEvent) {
    event.preventDefault();
    if (!text.trim()) return;
    setReadingError(null);
    setReading(readInstruction(text, tools));
  }

  async function confirmReading() {
    if (!reading) return;
    setReadingError(null);
    try {
      for (const item of reading) {
        await save.mutateAsync({ toolKey: item.toolKey, level: item.level });
      }
      setReading(null);
      setText("");
    } catch (error) {
      setReadingError(describeGuardrailSaveError(error, t));
    }
  }

  /** Every write to one row goes through here: on failure that row says so
   * in one sentence under it (never swallowed, never a toast alone). A picked
   * level already gets the table's own "back to what it was" line, so it
   * passes no `failure` and only clears an earlier one. */
  async function report(
    toolKey: string,
    write: () => Promise<unknown>,
    failure?: string,
  ): Promise<boolean> {
    try {
      await write();
      setRowError(({ [toolKey]: _gone, ...rest }) => rest);
      return true;
    } catch (error) {
      if (failure !== undefined) {
        setRowError((e) => ({
          ...e,
          [toolKey]: `${failure} ${describeGuardrailSaveError(error, t)}`,
        }));
      }
      return false;
    }
  }

  const agentName = data?.agentName ?? "";
  const rows: ToolGuardrailRow[] = tools.map((tool: MyToolGuardrail) => ({
    toolKey: tool.toolKey,
    connection: undefined,
    ceilingPolicy: toValue(tool.base),
    ownValue: toValue(tool.mine ?? tool.base),
    status: null,
    level: tool.level,
    ceilingLevel: tool.baseLevel,
    limits: tool.hasLimits,
    effect: effectSentence(tool.level, tool.toolKey, agentName, t),
    error: rowError[tool.toolKey],
    source: rowSource(tool.source),
    ceilingSource: tool.source === "agent" ? "agent" : "department",
  }));

  return (
    <div className="space-y-4">
      {isLoading || !data ? (
        <SkeletonRows />
      ) : (
        <>
          <div aria-live="polite" className="space-y-1 text-sm leading-relaxed">
            {summarySentences(data.summary, agentName, t).map((sentence) => (
              <p key={sentence}>{sentence}</p>
            ))}
          </div>

          <form onSubmit={onRead} className="space-y-2">
            <label htmlFor={fieldId} className="block text-sm font-medium">
              {t("What should never happen without you?", "Was soll nie ohne dich passieren?")}
            </label>
            <div className="flex gap-2">
              <input
                id={fieldId}
                ref={fieldRef}
                value={text}
                onChange={(e) => {
                  setText(e.target.value);
                  setReading(null);
                }}
                placeholder={t(
                  "e.g. Never send a mail without me",
                  "z. B. Nie eine Mail ohne mich senden",
                )}
                className="min-h-11 flex-1 rounded-md border border-border bg-background px-3 text-sm outline-none focus:border-primary/50"
              />
              <Button
                type="submit"
                variant="outline"
                className="min-h-11 min-w-11"
                aria-label={t("Read it", "Lesen")}
              >
                <CornerDownLeft aria-hidden className="h-4 w-4" />
              </Button>
            </div>
            {empty && !reading && (
              <p className="text-sm text-muted-foreground">
                {t(
                  "No guardrails of your own yet. Say what should never happen without you.",
                  "Noch keine eigenen Guardrails. Sag, was nie ohne dich passieren soll.",
                )}
              </p>
            )}
          </form>

          {reading && reading.length > 0 && (
            <div aria-live="polite" className="space-y-2 rounded-md border border-border p-3">
              <p className="text-sm font-medium">
                {t("Here's how I read it:", "So verstehe ich das:")}
              </p>
              <ul className="space-y-1 text-sm">
                {reading.map((item) => (
                  <li key={item.toolKey}>
                    {`${toolLabel(item.toolKey)} → ${levelLabel(item.level, "member", t)}`}
                  </li>
                ))}
              </ul>
              {readingError && <p className="text-xs text-destructive">{readingError}</p>}
              <div className="flex gap-2">
                <Button
                  className="min-h-11"
                  onClick={() => void confirmReading()}
                  disabled={save.isPending}
                >
                  {t("Confirm", "Bestätigen")}
                </Button>
                <Button variant="ghost" className="min-h-11" onClick={() => setReading(null)}>
                  {t("Cancel", "Abbrechen")}
                </Button>
              </div>
            </div>
          )}

          {reading && reading.length === 0 && (
            <div aria-live="polite" className="space-y-2 rounded-md border border-border p-3">
              <p className="text-sm">
                {t("I couldn't match that to a tool.", "Das konnte ich keinem Werkzeug zuordnen.")}
              </p>
              <p className="text-xs text-muted-foreground">{t("For example:", "Zum Beispiel:")}</p>
              <div className="flex flex-wrap gap-2">
                {instructionExamples(tools, t).map((example) => (
                  <button
                    key={example}
                    type="button"
                    data-testid="guardrail-example"
                    onClick={() => {
                      setText(example);
                      setReading(null);
                      fieldRef.current?.focus();
                    }}
                    className="min-h-11 rounded-full border border-border px-3 text-sm text-muted-foreground transition hover:text-foreground"
                  >
                    {example}
                  </button>
                ))}
              </div>
            </div>
          )}

          {tools.length > 0 && (
            <ToolGuardrailTable
              level="member"
              rows={rows}
              addableNames={[]}
              connections={[]}
              onSave={(toolKey, next) =>
                report(
                  toolKey,
                  () => save.mutateAsync({ toolKey, policy: toEntry(next) }),
                  t("Couldn't save this rule.", "Diese Regel konnte nicht gespeichert werden."),
                )
              }
              onSaveLevel={(toolKey, level) =>
                report(toolKey, () => save.mutateAsync({ toolKey, level }))
              }
              onReset={(toolKey) =>
                void report(
                  toolKey,
                  () => reset.mutateAsync({ toolKey }),
                  t(
                    "Couldn't reset this rule, so it still applies.",
                    "Diese Regel konnte nicht zurückgesetzt werden, sie gilt weiter.",
                  ),
                )
              }
              onAdd={() => {}}
              saving={save.isPending}
              agentId={agentId}
            />
          )}
        </>
      )}
    </div>
  );
}

/** The sheet around the panel: right-hand from 640px (≥480px wide), a
 * full-height bottom sheet below. Radix traps focus while it is open. */
export function MemberGuardrailsSheet({
  agentId,
  agentName,
  open,
  onOpenChange,
  trigger,
}: {
  agentId: string;
  agentName: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The control focus returns to on close. */
  trigger?: React.ReactNode;
}) {
  const t = useT();
  const narrow = useMediaQuery("(max-width: 639px)");
  // The same query the panel reads -- names the agent when the caller
  // (an approval card's "Change rule") only knew its id.
  const { data } = useMyGuardrails(open ? agentId : "");
  const name = agentName || data?.agentName || "";
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      {trigger && <SheetTrigger asChild>{trigger}</SheetTrigger>}
      <SheetContent
        side={narrow ? "bottom" : "right"}
        aria-describedby={undefined}
        className={cn(
          "flex flex-col gap-4 overflow-y-auto motion-reduce:transition-none",
          narrow ? "h-[100dvh]" : "w-[520px] max-w-[100vw] sm:max-w-[520px]",
        )}
      >
        <SheetHeader>
          <SheetTitle className="pr-8 text-left">
            {t("Your guardrails", "Deine Guardrails")}
            {name ? ` · ${name}` : ""}
          </SheetTitle>
        </SheetHeader>
        {open && <MemberGuardrailsPanel agentId={agentId} />}
      </SheetContent>
    </Sheet>
  );
}

/** The quiet "Your guardrails" entry in a chat header, with a count when the
 * member has rules of their own. Controlled from outside when a "Change rule"
 * link elsewhere in the chat should open the same sheet. */
export function YourGuardrailsButton({
  agentId,
  agentName,
  open: controlledOpen,
  onOpenChange,
}: {
  agentId: string;
  agentName: string;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  const t = useT();
  const { data, isError } = useMyGuardrails(agentId);
  const [innerOpen, setInnerOpen] = useState(false);
  const open = controlledOpen ?? innerOpen;
  const setOpen = onOpenChange ?? setInnerOpen;
  // 404: not an agent this member chats with -- nothing of theirs to show.
  if (isError) return null;
  const count = ownRuleCount(data);
  return (
    <MemberGuardrailsSheet
      agentId={agentId}
      agentName={agentName}
      open={open}
      onOpenChange={setOpen}
      trigger={
        <button
          type="button"
          className="inline-flex min-h-11 items-center gap-1.5 rounded-md px-2 text-xs text-muted-foreground transition hover:text-foreground"
        >
          <ShieldCheck aria-hidden className="h-3.5 w-3.5" />
          {t("Your guardrails", "Deine Guardrails")}
          {count > 0 && (
            <span className="rounded-full bg-muted px-1.5 text-[11px] text-foreground">
              {count}
            </span>
          )}
        </button>
      }
    />
  );
}

function memberLevelText(policy: ToolPolicy, t: (en: string, de: string) => string): string {
  const value = toValue(policy);
  const level = levelOf({ ...value, enabled: policy.enabled });
  const words =
    level === "ask_first" ? t("Asks them first", "Fragt erst sie") : levelLabel(level, "other", t);
  return hasLimits(value) ? `${words} ${t("with limits", "mit Grenzen")}` : words;
}

/** "For the agent": how many members tightened this agent for themselves,
 * opening a read-only list (member, tool, what was tightened). */
export function MemberGuardrailsInsight({ agentId }: { agentId: string }) {
  const t = useT();
  const { data: members = [] } = useMemberGuardrails(agentId);
  const [open, setOpen] = useState(false);
  const withRules = members.filter((member) => Object.keys(member.tools).length > 0);
  if (withRules.length === 0) return null;
  const label =
    withRules.length === 1
      ? t("1 member has their own guardrails", "1 Mitglied hat eigene Guardrails")
      : t(
          "{n} members have their own guardrails",
          "{n} Mitglieder haben eigene Guardrails",
        ).replace("{n}", String(withRules.length));
  return (
    <div className="space-y-2">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="min-h-11 rounded-md px-1 text-sm text-primary underline-offset-2 hover:underline"
      >
        {label}
      </button>
      {open && (
        <ul className="divide-y divide-border rounded-md border border-border text-sm">
          {withRules.map((member) => (
            <li key={member.memberId} className="space-y-1 p-3">
              <div className="font-medium">{member.memberName}</div>
              {Object.entries(member.tools).map(([key, policy]) => (
                <div key={key} className="text-muted-foreground">
                  {`${key}: ${memberLevelText(policy, t)}`}
                </div>
              ))}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
