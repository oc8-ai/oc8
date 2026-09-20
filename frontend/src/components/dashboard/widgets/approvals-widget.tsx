import { useMemo, useState } from "react";
import { toast } from "sonner";
import {
  AlertTriangle,
  ArrowRight,
  Check,
  ChevronRight,
  HelpCircle,
  KeyRound,
  Send,
  ShieldCheck,
  Sparkles,
  XCircle,
} from "lucide-react";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { AgentAvatar } from "@/components/agent-avatar";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  useAnswerClarification,
  useApprovals,
  useClarifications,
  useDecideApproval,
  useStanding,
  type Approval,
  type Clarification,
  type Seat,
} from "@/lib/hooks";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

// --------------------------------------------------------------------- model
// Kind, Row, DecidedRow, TENANT_WIDE, approvalRow, clarificationRow,
// relativeAge, mayActOn, ViewOnlyBecause, Pill, the
// kind/department filters, and the "Decided by you" list are carried over
// from the pre-dashboard `workspace.tsx` (git history), which had them
// inline rather than in a dedicated widget component.

type Kind = "approval" | "clarification";

interface Row {
  kind: Kind;
  id: string;
  title: string;
  agentId: string;
  agentName: string;
  departmentId: string | null;
  departmentName: string;
  amount: string | null;
  createdAt: string;
}

interface DecidedRow {
  id: string;
  kind: Kind;
  title: string;
  outcome: "approved" | "rejected" | "answered";
}

const TENANT_WIDE = "__tenant_wide__";

function approvalRow(a: Approval): Row {
  return {
    kind: "approval",
    id: a.id,
    title: a.title,
    agentId: a.agentId,
    agentName: a.agentName || a.agentId,
    departmentId: a.departmentId ?? null,
    departmentName: a.departmentName ?? "",
    amount: a.amount ?? null,
    createdAt: a.createdAt ?? "",
  };
}

function clarificationRow(c: Clarification): Row {
  return {
    kind: "clarification",
    id: c.id,
    title: c.question,
    agentId: c.agentId,
    agentName: c.agentName || c.agentId,
    departmentId: c.departmentId,
    departmentName: c.departmentName,
    amount: null,
    createdAt: c.createdAt,
  };
}

/** Relative age from an ISO timestamp. Empty for an empty or unparseable one —
 *  a row that says "0m" because a field was blank is a row that lies about how
 *  long somebody has been waiting. */
function relativeAge(iso: string): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const mins = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (mins < 60) return `${mins}m`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h`;
  return `${Math.round(hrs / 24)}d`;
}

/** Whether the caller may act on a row, and the wire now says exactly.
 *
 * `decidesEverywhere` and not `unrestricted`. The two are different people and
 * the backend has always known it — `authz/scope.py` carries two flags and
 * documents at length why they must not become one — but `/me` sent only the
 * first, so this returned true for an `auditor`, who holds `approval:view_any`,
 * sees every department, and is 403'd by `require_departmental(approval:decide)`
 * on every click. The role's entire value is that its account cannot have caused
 * what it is auditing; offering it Approve was the screen contradicting that.
 *
 * Below that, authority is only the seats, so the seat role in that row's own
 * department is the whole answer: a `dept_viewer` reads the queue and may not
 * empty it, and hiding the buttons is the honest way to say so.
 */
function mayActOn(seats: Seat[], decidesEverywhere: boolean, departmentId: string | null): boolean {
  if (decidesEverywhere) return true;
  if (!departmentId) return false;
  return seats.some((s) => s.departmentId === departmentId && s.seatRole === "dept_approver");
}

type ViewOnlyBecause = "role" | "seat";

// ---------------------------------------------------------------- the widget

export function ApprovalsWidget({
  initialSelectedId,
}: {
  config: Record<string, unknown>;
  onConfigChange: (config: Record<string, unknown>) => void;
  initialSelectedId?: string;
}) {
  const t = useT();
  const standing = useStanding();

  const approvalsQuery = useApprovals("pending");
  const clarificationsQuery = useClarifications();
  const approvals = useMemo(() => approvalsQuery.data ?? [], [approvalsQuery.data]);
  const clarifications = useMemo(() => clarificationsQuery.data ?? [], [clarificationsQuery.data]);

  const [selectedId, setSelectedId] = useState<string | null>(initialSelectedId ?? null);
  const [kindFilter, setKindFilter] = useState<"all" | Kind>("all");
  const [deptFilter, setDeptFilter] = useState<string | null>(null);
  const [decided, setDecided] = useState<DecidedRow[]>([]);

  const decide = useDecideApproval();
  const answer = useAnswerClarification();

  const decidedIds = useMemo(() => new Set(decided.map((d) => d.id)), [decided]);

  const rows = useMemo(() => {
    const all = [...approvals.map(approvalRow), ...clarifications.map(clarificationRow)].filter(
      (r) => !decidedIds.has(r.id),
    );
    return all.sort((a, b) => b.createdAt.localeCompare(a.createdAt) || b.id.localeCompare(a.id));
  }, [approvals, clarifications, decidedIds]);

  // The departments worth offering as a filter are the ones with work in
  // them, not every department that exists: a filter option that always
  // shows an empty list is furniture, and the employee cannot read
  // /departments anyway.
  const departmentOptions = useMemo(() => {
    const seen = new Map<string, string>();
    for (const r of rows) {
      const key = r.departmentId ?? TENANT_WIDE;
      if (!seen.has(key)) seen.set(key, r.departmentName);
    }
    return [...seen.entries()].map(([id, name]) => ({ id, name }));
  }, [rows]);

  // A department filter only counts while its pill is on screen. Deciding
  // the last item in a department drops it out of `departmentOptions`, and
  // the pill row disappears entirely once one department is left —
  // without this the filter would go on excluding rows with no visible
  // control to clear it.
  const activeDept =
    deptFilter && departmentOptions.length > 1 && departmentOptions.some((d) => d.id === deptFilter)
      ? deptFilter
      : null;

  const filtered = useMemo(
    () =>
      rows.filter((r) => {
        if (kindFilter !== "all" && r.kind !== kindFilter) return false;
        if (activeDept && (r.departmentId ?? TENANT_WIDE) !== activeDept) return false;
        return true;
      }),
    [rows, kindFilter, activeDept],
  );

  const current = rows.find((r) => r.id === selectedId) ?? null;
  const currentApproval =
    current?.kind === "approval" ? (approvals.find((a) => a.id === current.id) ?? null) : null;
  const currentClarification =
    current?.kind === "clarification"
      ? (clarifications.find((c) => c.id === current.id) ?? null)
      : null;

  const viewOnlyBecause: ViewOnlyBecause =
    standing.unrestricted && !standing.decidesEverywhere ? "role" : "seat";

  const loadError = approvalsQuery.error ?? clarificationsQuery.error;
  const loading = approvalsQuery.isPending || clarificationsQuery.isPending || standing.pending;

  async function onDecide(
    a: Approval,
    decision: "approve" | "reject",
    reason: string,
    option: string | null,
  ) {
    try {
      const result = await decide.mutateAsync({ approvalId: a.id, decision, reason, option });
      if (result?.resumed === false) {
        toast.warning(
          decision === "approve"
            ? t(
                "Recorded — but the agent's run could not be resumed, so the action never ran",
                "Erfasst — aber der Lauf des Agenten konnte nicht fortgesetzt werden, die Aktion wurde also nie ausgeführt",
              )
            : t(
                "Recorded — the run was already gone, so there was nothing to stop",
                "Erfasst — der Lauf war bereits beendet, es gab also nichts mehr zu stoppen",
              ),
        );
      } else {
        toast.success(
          decision === "approve" ? t("Approved", "Zugestimmt") : t("Rejected", "Abgelehnt"),
        );
      }
      setDecided((prev) => [
        {
          id: a.id,
          kind: "approval",
          title: a.title,
          outcome: decision === "approve" ? "approved" : "rejected",
        },
        ...prev,
      ]);
      setSelectedId(null);
    } catch (e) {
      toast.error(
        e instanceof Error
          ? e.message
          : t("Could not record the decision", "Entscheidung konnte nicht erfasst werden"),
      );
    }
  }

  async function onAnswer(c: Clarification, text: string) {
    try {
      await answer.mutateAsync({ clarificationId: c.id, answer: text });
      toast.success(t("Answer sent", "Antwort gesendet"));
      setDecided((prev) => [
        { id: c.id, kind: "clarification", title: c.question, outcome: "answered" },
        ...prev,
      ]);
      setSelectedId(null);
    } catch (e) {
      toast.error(
        e instanceof Error
          ? e.message
          : t("Could not send the answer", "Antwort konnte nicht gesendet werden"),
      );
    }
  }

  // "Nothing is waiting for you" and "nobody has assigned you to a
  // department" are the same blank screen otherwise, and one of them is the
  // system working while the other is a person locked out of their own job.
  if (standing.unassigned) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 p-4 text-center">
        <div className="grid h-9 w-9 place-items-center rounded-full bg-[color:var(--status-warning)]/15 text-[color:var(--status-warning)]">
          <KeyRound className="h-4.5 w-4.5" />
        </div>
        <div className="text-sm font-medium">
          {t("You are not assigned to a department", "Du bist keiner Abteilung zugeordnet")}
        </div>
        <p className="max-w-xs text-xs text-muted-foreground">
          {t(
            "Ask an administrator to add you — your queue appears the moment they do.",
            "Bitte einen Administrator, dich einzutragen — deine Liste erscheint, sobald das geschehen ist.",
          )}
        </p>
        {/* A plain anchor, not the router's `Link` -- this is the only spot
            in any widget that navigates away, and pulling in a router
            context is not worth it for one link out of a grid tile. */}
        <a
          href="/governance"
          className="mt-1 inline-flex items-center gap-1 text-xs text-primary hover:underline"
        >
          {t("What may I do?", "Was darf ich?")} <ArrowRight className="h-3 w-3" />
        </a>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {loadError && (
        <div className="flex items-start gap-2 border-b border-[color:var(--status-error)]/40 px-3 py-2 text-xs">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-[color:var(--status-error)]" />
          <div>
            <div>
              {t("Your queue could not be loaded", "Deine Liste konnte nicht geladen werden")}
            </div>
            {/* Named rather than swallowed: an empty list drawn over a
                failed fetch is the screen telling somebody there is
                nothing to do. */}
            <div className="mt-0.5 text-muted-foreground">
              {loadError instanceof Error ? loadError.message : String(loadError)}
            </div>
          </div>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-1.5 border-b border-border px-2 py-1.5">
        <Pill active={kindFilter === "all"} onClick={() => setKindFilter("all")}>
          {t("All", "Alles")}
        </Pill>
        <Pill active={kindFilter === "approval"} onClick={() => setKindFilter("approval")}>
          {t("Approvals", "Freigaben")}
        </Pill>
        <Pill
          active={kindFilter === "clarification"}
          onClick={() => setKindFilter("clarification")}
        >
          {t("Questions", "Rückfragen")}
        </Pill>
        {/* A picker over one department is furniture, so it only appears
            when there is actually something to tell apart. */}
        {departmentOptions.length > 1 && (
          <>
            <span className="mx-0.5 h-3.5 w-px bg-border" />
            <Pill active={activeDept === null} onClick={() => setDeptFilter(null)}>
              {t("Every department", "Alle Abteilungen")}
            </Pill>
            {departmentOptions.map((d) => (
              <Pill key={d.id} active={activeDept === d.id} onClick={() => setDeptFilter(d.id)}>
                {d.id === TENANT_WIDE
                  ? t("Company-wide", "Unternehmensweit")
                  : d.name || t("Unnamed department", "Unbenannte Abteilung")}
              </Pill>
            ))}
          </>
        )}
      </div>

      <div className="flex-1 overflow-y-auto">
        <div className="divide-y divide-border">
          {filtered.length === 0 && !loadError && (
            <div className="flex flex-col items-center gap-2 px-4 py-8 text-center">
              <Sparkles className="h-5 w-5 text-primary" />
              <div className="text-sm">
                {loading
                  ? t("Loading…", "Wird geladen…")
                  : rows.length > 0
                    ? t(
                        "Nothing under this filter. Your other departments still have work.",
                        "Unter diesem Filter nichts. In deinen anderen Abteilungen liegt noch Arbeit.",
                      )
                    : t("Nothing is waiting for you.", "Nichts wartet auf dich.")}
              </div>
            </div>
          )}
          {filtered.map((r) => (
            <QueueRow
              key={r.id}
              row={r}
              active={r.id === selectedId}
              showDepartment={departmentOptions.length > 1}
              onOpen={() => setSelectedId(r.id)}
            />
          ))}
        </div>

        {decided.length > 0 && (
          <div className="divide-y divide-border border-t border-border">
            <div className="px-3 py-1.5 text-[10px] uppercase tracking-widest text-muted-foreground">
              {t("Decided by you", "Von dir entschieden")}
            </div>
            {decided.map((d) => (
              <div key={d.id} className="flex items-center gap-2 px-3 py-2 text-xs">
                <span
                  className={cn(
                    "grid h-4 w-4 shrink-0 place-items-center rounded-full text-[9px]",
                    d.outcome === "rejected"
                      ? "bg-[color:var(--status-error)]/15 text-[color:var(--status-error)]"
                      : "bg-[color:var(--status-running)]/15 text-[color:var(--status-running)]",
                  )}
                >
                  {d.outcome === "rejected" ? "✕" : "✓"}
                </span>
                <span className="truncate text-muted-foreground">{d.title}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      <Dialog open={current !== null} onOpenChange={(open) => !open && setSelectedId(null)}>
        <DialogContent className="max-w-2xl p-0">
          <DialogHeader className="sr-only">
            <DialogTitle>
              {currentApproval?.title ?? currentClarification?.question ?? ""}
            </DialogTitle>
          </DialogHeader>
          {currentApproval ? (
            <ApprovalPane
              key={currentApproval.id}
              approval={currentApproval}
              mayAct={mayActOn(
                standing.seats,
                standing.decidesEverywhere,
                currentApproval.departmentId ?? null,
              )}
              viewOnlyBecause={viewOnlyBecause}
              busy={decide.isPending}
              onDecide={onDecide}
            />
          ) : currentClarification ? (
            <ClarificationPane
              key={currentClarification.id}
              clarification={currentClarification}
              mayAct={mayActOn(
                standing.seats,
                standing.decidesEverywhere,
                currentClarification.departmentId,
              )}
              viewOnlyBecause={viewOnlyBecause}
              busy={answer.isPending}
              onAnswer={onAnswer}
            />
          ) : null}
        </DialogContent>
      </Dialog>
    </div>
  );
}

function Pill({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-full border px-2 py-0.5 text-[11px] transition",
        active
          ? "border-primary/50 bg-primary/10 text-primary"
          : "border-border bg-panel text-muted-foreground hover:text-foreground",
      )}
    >
      {children}
    </button>
  );
}

// ------------------------------------------------------------------ the list
// QueueRow, ApprovalPane, ArgumentValue, ClarificationPane are verbatim from
// the old workspace.tsx (pre-rewrite lines 565-1019), with one change: each
// pane's outer wrapper keeps `flex h-full flex-col` so it fills the Dialog
// exactly as it filled the old inline Panel.

function QueueRow({
  row,
  active,
  showDepartment,
  onOpen,
}: {
  row: Row;
  active: boolean;
  showDepartment: boolean;
  onOpen: () => void;
}) {
  const t = useT();
  const age = relativeAge(row.createdAt);
  return (
    <button
      type="button"
      onClick={onOpen}
      className={cn(
        "flex w-full items-start gap-3 px-4 py-3 text-left transition",
        active ? "bg-primary/5" : "hover:bg-muted/30",
      )}
    >
      <AgentAvatar seed={row.agentId} size={36} background="squircle" ariaHidden />
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-2">
          {row.kind === "clarification" ? (
            <HelpCircle className="h-3.5 w-3.5 shrink-0 text-primary" />
          ) : (
            <ShieldCheck className="h-3.5 w-3.5 shrink-0 text-[color:var(--status-warning)]" />
          )}
          <span className="truncate text-sm font-medium">{row.title}</span>
          {row.amount && (
            <span className="shrink-0 rounded-full border border-border bg-background/60 px-1.5 py-0.5 font-mono text-[10px] tabular-nums">
              {row.amount}
            </span>
          )}
        </span>
        <span className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
          <span className="truncate">{row.agentName}</span>
          {showDepartment && (
            <>
              <span>·</span>
              <span className="truncate">
                {row.departmentId
                  ? row.departmentName || t("Unnamed department", "Unbenannte Abteilung")
                  : t("Company-wide", "Unternehmensweit")}
              </span>
            </>
          )}
          {age && (
            <>
              <span>·</span>
              <span>{age}</span>
            </>
          )}
        </span>
      </span>
      <ChevronRight
        className={cn("mt-1 h-4 w-4 shrink-0", active ? "text-primary" : "text-muted-foreground")}
      />
    </button>
  );
}

// Raw operator symbols (`ConditionOperator` in `authz/pdp.py`) shown as
// readable comparison signs rather than the wire tokens.
const OPERATOR_SYMBOLS: Record<string, string> = {
  ">": ">",
  ">=": "≥",
  "<": "<",
  "<=": "≤",
  "==": "=",
  "!=": "≠",
  in: "in",
  not_in: "not in",
};

// Mirrors `guardrail-function-rules.tsx`'s `humanizeName` -- no shared label
// dictionary exists anywhere in the stack, so this stays a small mechanical
// humanization local to each consumer rather than an import.
function humanizeAttribute(name: string): string {
  return name
    .split("_")
    .map((word) =>
      word.length <= 2 ? word.toUpperCase() : word.charAt(0).toUpperCase() + word.slice(1),
    )
    .join(" ");
}

function formatReasonValue(value: unknown): string {
  if (typeof value === "number") return value.toLocaleString();
  if (value == null) return "?";
  return String(value);
}

/** Turns `ApprovalDTO.reasonContext` (Decision.reason_code/context, see
 *  `authz/pdp.py`) into a translated sentence. `null` for anything not raised
 *  via `authorize_tool_call` or a `reason_code` this pane doesn't know yet --
 *  the caller falls back to the raw `detail` string in that case. */
function formatReasonContext(
  reasonContext: Approval["reasonContext"],
  t: (en: string, de: string) => string,
): string | null {
  if (!reasonContext || typeof reasonContext.code !== "string") return null;
  switch (reasonContext.code) {
    case "condition_matched": {
      const attribute = humanizeAttribute(String(reasonContext.attribute ?? ""));
      const operator = OPERATOR_SYMBOLS[String(reasonContext.operator ?? "")] ?? "?";
      const threshold = formatReasonValue(reasonContext.threshold);
      const actual = formatReasonValue(reasonContext.actual);
      return t(
        `A configured guardrail applies: ${attribute} ${operator} ${threshold} -- this one is ${actual}.`,
        `Es greift eine konfigurierte Guardrail: ${attribute} ${operator} ${threshold} -- dieser Wert liegt bei ${actual}.`,
      );
    }
    case "always_requires_approval": {
      const tool = reasonContext.tool ? humanizeAttribute(String(reasonContext.tool)) : "";
      return t(
        `This action${tool ? ` (${tool})` : ""} always requires approval, regardless of amount.`,
        `Diese Aktion${tool ? ` (${tool})` : ""} erfordert immer eine Freigabe, unabhängig vom Betrag.`,
      );
    }
    case "value_threshold_exceeded": {
      const threshold = formatReasonValue(reasonContext.threshold);
      const actual = formatReasonValue(reasonContext.actual);
      return t(
        `This exceeds the autonomous limit of ${threshold} -- this one is ${actual}.`,
        `Dies überschreitet das eigenständige Limit von ${threshold} -- dieser Wert liegt bei ${actual}.`,
      );
    }
    default:
      return null;
  }
}

function ApprovalPane({
  approval,
  mayAct,
  viewOnlyBecause,
  busy,
  onDecide,
}: {
  approval: Approval;
  mayAct: boolean;
  viewOnlyBecause: ViewOnlyBecause;
  busy: boolean;
  onDecide: (
    a: Approval,
    decision: "approve" | "reject",
    reason: string,
    option: string | null,
  ) => void;
}) {
  const t = useT();
  const options = approval.options ?? [];
  const [choice, setChoice] = useState<string | null>(
    approval.recommendation ?? (options.length === 1 ? options[0].key : null),
  );
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState(false);

  const toolArguments = Object.entries(approval.toolArguments ?? {});
  // Truthiness, not `!= null`: `amount_text` is a free-text column and an empty
  // string is not an amount. Testing for null alone would render a big blank
  // where the number goes AND put a confirm dialog in front of an approval that
  // has no number on it at all.
  const hasAmount = !!approval.amount;
  const needsConfirm = hasAmount || approval.actionType === "tool_send";
  const canReject = reason.trim().length > 0;

  const approve = () => onDecide(approval, "approve", reason, choice);
  const reasonSentence = formatReasonContext(approval.reasonContext, t);

  return (
    <div className="flex h-full max-h-[80vh] flex-col">
      <div className="flex-1 space-y-6 overflow-y-auto px-6 py-5">
        <section>
          <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
            <AgentAvatar seed={approval.agentId} size={20} ariaHidden />
            <span className="font-medium text-foreground">
              {approval.agentName || approval.agentId}
            </span>
            {approval.departmentName && (
              <>
                <ArrowRight className="h-3 w-3" />
                <span>{approval.departmentName}</span>
              </>
            )}
            {approval.taskTitle && (
              <>
                <ArrowRight className="h-3 w-3" />
                <span className="truncate">{approval.taskTitle}</span>
              </>
            )}
          </div>
          <h2 className="mt-2 font-serif text-2xl leading-tight">{approval.title}</h2>
          {reasonSentence ? (
            <div className="mt-2 flex items-start gap-2 rounded-md border border-border bg-panel/60 px-3 py-2 text-sm leading-relaxed text-foreground/90">
              <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-primary" aria-hidden />
              <span>{reasonSentence}</span>
            </div>
          ) : (
            approval.detail && (
              <p className="mt-2 text-sm leading-relaxed text-foreground/90">{approval.detail}</p>
            )
          )}
        </section>

        {(approval.toolName || options.length > 0) && (
          <section>
            <div className="mb-2 text-[10px] uppercase tracking-widest text-muted-foreground">
              {t("What happens if you agree", "Was passiert, wenn du zustimmst")}
            </div>

            {approval.toolName && (
              <div className="rounded-md border border-border bg-panel/60">
                <div className="flex items-center gap-2 border-b border-border px-3 py-2">
                  <Send className="h-3.5 w-3.5 shrink-0 text-primary" />
                  <code className="truncate font-mono text-sm">{approval.toolName}</code>
                </div>
                {toolArguments.length > 0 ? (
                  <dl className="divide-y divide-border/60">
                    {toolArguments.map(([k, v]) => (
                      <div
                        key={k}
                        className="grid grid-cols-1 gap-0.5 px-3 py-2 sm:grid-cols-[minmax(0,10rem)_1fr] sm:gap-3"
                      >
                        <dt className="text-[10px] uppercase tracking-widest text-muted-foreground sm:pt-0.5">
                          {k}
                        </dt>
                        <dd className="min-w-0 font-mono text-xs">
                          <ArgumentValue value={v} />
                        </dd>
                      </div>
                    ))}
                  </dl>
                ) : (
                  <div className="px-3 py-2 text-xs text-muted-foreground">
                    {t("Called with no arguments.", "Wird ohne Argumente aufgerufen.")}
                  </div>
                )}
              </div>
            )}

            {options.length > 0 && (
              <div className={cn("space-y-2", approval.toolName && "mt-3")}>
                {options.map((o) => {
                  const active = o.key === choice;
                  return (
                    <button
                      key={o.key}
                      type="button"
                      onClick={() => setChoice(o.key)}
                      className={cn(
                        "flex w-full items-start gap-3 rounded-md border px-3 py-2.5 text-left transition",
                        active
                          ? "border-primary/60 bg-primary/5"
                          : "border-border hover:bg-muted/30",
                      )}
                    >
                      <span
                        className={cn(
                          "mt-1 h-3 w-3 shrink-0 rounded-full border",
                          active ? "border-primary bg-primary" : "border-muted-foreground/50",
                        )}
                      />
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center gap-2 text-sm font-medium">
                          {o.label}
                          {o.key === approval.recommendation && (
                            <span className="rounded-full border border-border px-1.5 py-0.5 text-[10px] font-normal text-muted-foreground">
                              {t("agent's suggestion", "Vorschlag des Agenten")}
                            </span>
                          )}
                        </span>
                        {o.detail && (
                          <span className="mt-0.5 block text-xs text-muted-foreground">
                            {o.detail}
                          </span>
                        )}
                      </span>
                    </button>
                  );
                })}
              </div>
            )}
          </section>
        )}

        {hasAmount && (
          <section>
            <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
              {t("Amount", "Betrag")}
            </div>
            <div className="mt-1 font-mono text-3xl tabular-nums">{approval.amount}</div>
          </section>
        )}

        {mayAct && (
          <section>
            <label
              htmlFor="dashboard-approval-reason"
              className="mb-2 block text-[10px] uppercase tracking-widest text-muted-foreground"
            >
              {t("Reason", "Begründung")}
              <span className="ml-1 normal-case tracking-normal text-muted-foreground/80">
                {t("— required to reject", "— zum Ablehnen erforderlich")}
              </span>
            </label>
            <textarea
              id="dashboard-approval-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              rows={3}
              placeholder={t(
                "e.g. discount too high for this customer",
                "z. B. Rabatt für diesen Kunden zu hoch",
              )}
              className="w-full resize-none rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
            />
          </section>
        )}
      </div>

      <footer className="sticky bottom-0 flex flex-wrap items-center gap-2 border-t border-border bg-panel px-6 py-3">
        {!mayAct ? (
          <p className="text-xs text-muted-foreground">
            {viewOnlyBecause === "role"
              ? t(
                  "You can read every department, but decide in none — your role is read-only.",
                  "Du kannst alle Abteilungen einsehen, aber in keiner entscheiden — deine Rolle ist nur lesend.",
                )
              : t(
                  "You can read this one, but not decide it — your seat in this department is view-only.",
                  "Du kannst diesen Vorgang einsehen, aber nicht entscheiden — dein Platz in dieser Abteilung ist nur lesend.",
                )}
          </p>
        ) : (
          <>
            <button
              type="button"
              onClick={() => onDecide(approval, "reject", reason, null)}
              disabled={busy || !canReject}
              title={
                canReject
                  ? undefined
                  : t(
                      "Give a reason first — a rejection with none is a dead end for the agent that has to act on it",
                      "Bitte zuerst begründen — eine Ablehnung ohne Begründung ist eine Sackgasse für den Agenten, der damit weiterarbeiten muss",
                    )
              }
              className="inline-flex items-center gap-1.5 rounded-md border border-[color:var(--status-error)]/40 bg-[color:var(--status-error)]/10 px-3 py-2 text-sm text-[color:var(--status-error)] transition hover:brightness-110 disabled:opacity-40"
            >
              <XCircle className="h-4 w-4" /> {t("Reject", "Ablehnen")}
            </button>
            <button
              type="button"
              onClick={() => (needsConfirm ? setConfirming(true) : approve())}
              disabled={busy}
              className="ml-auto inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:brightness-110 glow-teal disabled:opacity-50"
            >
              <Check className="h-4 w-4" /> {t("Approve", "Zustimmen")}
            </button>
          </>
        )}
      </footer>

      <AlertDialog open={confirming} onOpenChange={setConfirming}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("Approve this?", "Wirklich zustimmen?")}</AlertDialogTitle>
            <AlertDialogDescription>
              {hasAmount
                ? t(
                    `Approving releases ${approval.amount}. The agent carries on immediately and this cannot be taken back.`,
                    `Mit der Zustimmung werden ${approval.amount} freigegeben. Der Agent macht sofort weiter, und das lässt sich nicht zurücknehmen.`,
                  )
                : t(
                    `Approving lets the agent run ${approval.toolName ?? "this action"} now. It sends something outward and cannot be taken back.`,
                    `Mit der Zustimmung führt der Agent ${approval.toolName ?? "diese Aktion"} sofort aus. Dabei geht etwas nach außen, und das lässt sich nicht zurücknehmen.`,
                  )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("Cancel", "Abbrechen")}</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setConfirming(false);
                approve();
              }}
            >
              {t("Yes, approve", "Ja, zustimmen")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function ArgumentValue({ value }: { value: unknown }) {
  if (value === null || value === undefined)
    return <span className="text-muted-foreground">—</span>;
  if (typeof value === "object") {
    return (
      <pre className="overflow-x-auto whitespace-pre-wrap break-words text-xs leading-relaxed">
        {JSON.stringify(value, null, 2)}
      </pre>
    );
  }
  return <span className="break-words">{String(value)}</span>;
}

function ClarificationPane({
  clarification,
  mayAct,
  viewOnlyBecause,
  busy,
  onAnswer,
}: {
  clarification: Clarification;
  mayAct: boolean;
  viewOnlyBecause: ViewOnlyBecause;
  busy: boolean;
  onAnswer: (c: Clarification, answer: string) => void;
}) {
  const t = useT();
  const [text, setText] = useState("");
  const canSend = text.trim().length > 0;

  return (
    <div className="flex h-full max-h-[80vh] flex-col">
      <div className="flex-1 space-y-6 overflow-y-auto px-6 py-5">
        <section>
          <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
            <AgentAvatar seed={clarification.agentId} size={20} ariaHidden />
            <span className="font-medium text-foreground">
              {clarification.agentName || clarification.agentId}
            </span>
            {clarification.departmentName && (
              <>
                <ArrowRight className="h-3 w-3" />
                <span>{clarification.departmentName}</span>
              </>
            )}
          </div>
          <div className="mt-2 text-[10px] uppercase tracking-widest text-muted-foreground">
            {t("Waiting on your answer", "Wartet auf deine Antwort")}
          </div>
          <p className="mt-1 text-sm text-foreground/90">{clarification.question}</p>
        </section>

        {mayAct && (
          <section>
            <label
              htmlFor="dashboard-clarification-answer"
              className="mb-2 block text-[10px] uppercase tracking-widest text-muted-foreground"
            >
              {t("Your answer", "Deine Antwort")}
            </label>
            <textarea
              id="dashboard-clarification-answer"
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={4}
              placeholder={t("Answer in your own words", "Antworte in eigenen Worten")}
              className="w-full resize-none rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
            />
          </section>
        )}
      </div>

      <footer className="sticky bottom-0 flex items-center justify-end gap-2 border-t border-border bg-panel px-6 py-3">
        {!mayAct ? (
          <p className="mr-auto text-xs text-muted-foreground">
            {viewOnlyBecause === "role"
              ? t(
                  "You can read every department's questions, but answer none — your role is read-only.",
                  "Du kannst die Fragen aller Abteilungen lesen, aber keine beantworten — deine Rolle ist nur lesend.",
                )
              : t(
                  "You can read this question, but not answer it — your seat in this department is view-only.",
                  "Du kannst diese Frage lesen, aber nicht beantworten — dein Platz in dieser Abteilung ist nur lesend.",
                )}
          </p>
        ) : (
          <button
            type="button"
            onClick={() => onAnswer(clarification, text)}
            disabled={busy || !canSend}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:brightness-110 glow-teal disabled:opacity-40"
          >
            <Check className="h-4 w-4" /> {t("Answer", "Antworten")}
          </button>
        )}
      </footer>
    </div>
  );
}
