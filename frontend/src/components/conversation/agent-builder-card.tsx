// "Agent anlegen" (Copilot Agent Builder §2.3/§3.3): the Copilot's proposal for
// one new agent, from the proposal DTO. The member creates it, sends it for
// approval, or keeps typing to adjust it -- the Copilot then replaces this card
// with a new one and this one collapses to a line. In an approver's pane it is
// read-only.
import { useNavigate, Link } from "@tanstack/react-router";
import { Bot } from "lucide-react";
import { useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  builderRefusalCode,
  builderRefusalText,
  useAgentBuilder,
  useApplyAgentBuilder,
  useRequestAgentBuilder,
  type AgentBuilderDTO,
  type BuilderTriggerDTO,
} from "@/lib/agent-builder-api";
import { describeCron } from "@/lib/describe-cron";
import { useMay } from "@/lib/governance-hooks";
import { useRunAgent } from "@/lib/hooks";
import { useT } from "@/lib/i18n";
import type { Translate } from "@/lib/tool-labels";
import { cn } from "@/lib/utils";

function whenText(trigger: BuilderTriggerDTO, t: Translate): string {
  if (trigger.kind === "cron" && trigger.cronExpression) {
    // The scheduler fires crons in UTC (triggers/service.compute_next_run).
    return describeCron(trigger.cronExpression, t, "UTC");
  }
  if (trigger.kind === "event") {
    return t(
      `When ${trigger.eventSource ?? ""} reports ${trigger.eventType ?? ""}`,
      `Wenn ${trigger.eventSource ?? ""} ${trigger.eventType ?? ""} meldet`,
    );
  }
  return t("On demand", "Auf Abruf");
}

function decisionText(decision: string, t: Translate): string {
  switch (decision) {
    case "approval_required":
      return t("asks first", "fragt vorher");
    case "not_allowed":
      return t("never", "nie");
    case "with_limits":
      return t("within limits", "in Grenzen");
    default:
      return t("on its own", "selbstständig");
  }
}

function StatusLine({ card, t }: { card: AgentBuilderDTO; t: Translate }) {
  const map: Record<string, { dot: string; text: string }> = {
    draft: { dot: "bg-primary", text: t("Proposal", "Vorschlag") },
    awaiting_approval: {
      dot: "bg-status-warning",
      text: t("Waiting for approval", "Wartet auf Freigabe"),
    },
    applied: { dot: "bg-status-running", text: t("Created", "Angelegt") },
    rejected: { dot: "bg-status-error", text: t("Rejected", "Abgelehnt") },
    expired: { dot: "bg-status-paused", text: t("Expired", "Abgelaufen") },
  };
  const s = map[card.status] ?? map.draft;
  return (
    <span className="inline-flex items-center gap-1.5 text-micro" role="status">
      <span className={cn("size-2 rounded-full", s.dot)} aria-hidden />
      {s.text}
    </span>
  );
}

export function AgentBuilderCard({
  proposalId,
  readOnly = false,
  heading,
  onPropose,
}: {
  proposalId: string;
  readOnly?: boolean;
  /** A label above the card, shown only once the card itself is. */
  heading?: string;
  /** "Neu vorschlagen" on an expired card: sends a message to the Copilot. */
  onPropose?: (text: string) => void;
}) {
  const t = useT();
  const may = useMay();
  const navigate = useNavigate();
  const { data: card, isLoading, isError } = useAgentBuilder(proposalId);
  const apply = useApplyAgentBuilder(proposalId);
  const request = useRequestAgentBuilder(proposalId);
  const runAgent = useRunAgent();
  const [testFailed, setTestFailed] = useState(false);

  // Not found, or not this member's to see: a note, never a skeleton that
  // never resolves.
  if (isError && !card) {
    return (
      <p className="text-xs text-muted-foreground">
        {t("Preview unavailable", "Vorschau nicht verfügbar")}
      </p>
    );
  }
  if (isLoading || !card) return <Skeleton className="h-32 w-full rounded-lg" />;
  if (card.status === "superseded") {
    return (
      <p className="text-xs text-muted-foreground">
        {t("Replaced by a newer proposal", "Durch einen neueren Vorschlag ersetzt")}
      </p>
    );
  }
  const actionsAllowed = !readOnly && !card.readOnly;
  // A refusal stored on the proposal (read back after a refetch) wins over the
  // mutation's own error; any other failure still gets the generic text.
  const clientError = apply.error ?? request.error;
  const clientCode = clientError ? (builderRefusalCode(clientError) ?? "failed") : null;
  const refusal = card.refusal ?? clientCode;
  const testTask =
    card.triggers[0]?.taskText ||
    t(
      "Do a first test run and tell me briefly what you did.",
      "Mach einen ersten Probelauf und sag mir kurz, was du getan hast.",
    );

  const rows: { label: string; value: ReactNode }[] = [
    {
      label: t("Department", "Abteilung"),
      value:
        card.newDepartment && card.status !== "applied"
          ? t(`New department: ${card.departmentName}`, `Neue Abteilung: ${card.departmentName}`)
          : card.departmentName,
    },
    ...(card.model ? [{ label: t("Model", "Modell"), value: card.model.label }] : []),
    ...(card.skills.length
      ? [{ label: t("Skills", "Skills"), value: card.skills.map((s) => s.label).join(", ") }]
      : []),
    {
      label: t("When it works", "Wann er arbeitet"),
      value: card.triggers.length
        ? card.triggers.map((tr) => whenText(tr, t)).join(", ")
        : t("On demand", "Auf Abruf"),
    },
    ...(card.systems.length
      ? [{ label: t("Connected systems", "Systeme"), value: card.systems.join(", ") }]
      : []),
    ...(card.limits.length
      ? [
          {
            label: t("Never without approval", "Nie ohne Freigabe"),
            value: card.limits
              .map((l) => `${l.connectionName} · ${l.function}: ${decisionText(l.decision, t)}`)
              .join("; "),
          },
        ]
      : []),
  ];

  return (
    <>
      {heading && <div className="mb-2 text-micro">{heading}</div>}
      <section
        className="space-y-3 rounded-lg border border-border bg-card p-4"
        aria-label={t("New agent", "Neuer Agent")}
      >
        <div className="flex items-center justify-between gap-2">
          <span className="inline-flex items-center gap-2 text-micro">
            <Bot className="size-4 text-primary" aria-hidden />
            {t("New agent", "Neuer Agent")}
          </span>
          <StatusLine card={card} t={t} />
        </div>
        <div>
          <h3 className="text-base font-semibold">{card.name}</h3>
          {card.roleTitle && <p className="text-xs text-muted-foreground">{card.roleTitle}</p>}
        </div>
        <dl className="grid grid-cols-1 gap-x-3 gap-y-1 text-sm sm:grid-cols-[minmax(0,10rem)_1fr]">
          {rows.map((row) => (
            <div key={row.label} className="contents">
              <dt className="text-micro sm:pt-0.5">{row.label}</dt>
              <dd className="min-w-0">{row.value}</dd>
            </div>
          ))}
        </dl>
        {card.mission && (
          <details className="text-sm">
            <summary className="min-h-9 cursor-pointer text-muted-foreground">
              {t("Mission", "Auftrag")}
            </summary>
            <p className="mt-1 whitespace-pre-wrap">{card.mission}</p>
          </details>
        )}
        {refusal && card.status === "draft" && (
          <p role="alert" className="text-xs text-status-error">
            {builderRefusalText(refusal, card.failedStep, t, card.failedReason)}
          </p>
        )}
        {card.status === "rejected" && (
          <p className="text-sm">
            {card.rejectedReason
              ? t(`Rejected: ${card.rejectedReason}`, `Abgelehnt: ${card.rejectedReason}`)
              : t("Rejected", "Abgelehnt")}
          </p>
        )}
        {card.status === "applied" && card.agent && (
          <div className="space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <Link
                to="/agents/$id"
                params={{ id: card.agent.id }}
                className="text-sm text-primary underline-offset-2 hover:underline"
              >
                {t(`Created: ${card.agent.name}`, `Angelegt: ${card.agent.name}`)}
              </Link>
              {actionsAllowed && may("run:start") && card.agent.status !== "pending_approval" && (
                <Button
                  variant="outline"
                  disabled={runAgent.isPending}
                  onClick={() => {
                    const agentId = card.agent!.id;
                    setTestFailed(false);
                    runAgent.mutate(
                      { agentId, task: testTask },
                      {
                        onSuccess: () =>
                          void navigate({
                            to: "/agents/$id",
                            params: { id: agentId },
                            search: { tab: "live" },
                          }),
                        onError: () => setTestFailed(true),
                      },
                    );
                  }}
                >
                  {t("Test now", "Jetzt testen")}
                </Button>
              )}
            </div>
            {/* The tenant's hire gate holds it until somebody approves the hire. */}
            {card.agent.status === "pending_approval" && (
              <p className="text-xs text-muted-foreground">
                {t("Waiting for hiring approval", "Wartet auf Einstellungsfreigabe")}
              </p>
            )}
            {testFailed && (
              <p role="alert" className="text-xs text-status-error">
                {t("Could not start the test", "Konnte den Test nicht starten")}
              </p>
            )}
          </div>
        )}
        {card.status === "expired" && actionsAllowed && onPropose && (
          <Button
            variant="outline"
            onClick={() =>
              onPropose(
                t(
                  `/agent Please propose ${card.name} again.`,
                  `/agent Schlag ${card.name} bitte noch einmal vor.`,
                ),
              )
            }
          >
            {t("Propose again", "Neu vorschlagen")}
          </Button>
        )}
        {card.status === "draft" && actionsAllowed && (
          <div className="space-y-2">
            <div className="flex flex-wrap gap-2">
              {card.mayCreate ? (
                <Button disabled={apply.isPending} onClick={() => apply.mutate()}>
                  {t("Create this agent", "Anlegen")}
                </Button>
              ) : (
                card.mayRequest && (
                  <Button disabled={request.isPending} onClick={() => request.mutate()}>
                    {t("Send for approval", "Zur Freigabe senden")}
                  </Button>
                )
              )}
            </div>
            <p className="text-xs text-muted-foreground">
              {t(
                "To adjust it, just keep typing what should be different.",
                "Zum Anpassen schreib einfach weiter, was anders sein soll.",
              )}
            </p>
          </div>
        )}
      </section>
    </>
  );
}
