import { useState } from "react";
import { ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { levelLabel } from "@/lib/guardrail-levels";
import { useDecideApproval, type Approval } from "@/lib/hooks";
import { useApprovalFollowUp, useMyGuardrails } from "@/lib/hooks-member-guardrails";
import { useT } from "@/lib/i18n";
import { approvalActionText, toolLabel } from "@/lib/member-guardrail-text";

// The approval a member's own rule asked for, where they are (D3a, design
// review D4): in the conversation, in the Copilot's "Waiting" tab. Decides
// through the same `useDecideApproval` the inbox's `ApprovalPane` uses. After
// the member's yes, the department's second stage (a new row whose
// `previousApprovalId` is this one) is followed when the member can see it;
// when they cannot -- no seat in that department -- a decision that did not
// resume the run is what says the department is next.
export function ChatApprovalCard({
  approval,
  onChangeRule,
}: {
  approval: Approval;
  /** Opens "Your guardrails" for the approval's agent. */
  onChangeRule?: (agentId: string) => void;
}) {
  const t = useT();
  const decide = useDecideApproval();
  const [result, setResult] = useState<Approval | null>(null);
  const [failed, setFailed] = useState(false);
  const own = result ?? approval;
  const { data: followUp } = useApprovalFollowUp(own.status === "approved" ? own.id : null);
  const { data: guardrails } = useMyGuardrails(approval.agentId);

  const action = approvalActionText(approval, t);
  const toolKey =
    typeof approval.reasonContext?.connection === "string"
      ? approval.reasonContext.connection
      : (approval.toolName ?? "");
  const ruleLevel =
    guardrails?.tools.find((tool) => tool.toolKey === toolKey)?.level ?? "ask_first";
  const subject = approval.toolArguments?.subject;
  const department = approval.departmentName || t("the department", "die Abteilung");

  async function decideAs(decision: "approve" | "reject") {
    setFailed(false);
    try {
      setResult(await decide.mutateAsync({ approvalId: approval.id, decision }));
    } catch {
      setFailed(true);
    }
  }

  let outcome: string | null = null;
  if (followUp?.status === "rejected") {
    outcome = t("Rejected by {dept}", "Abgelehnt von {dept}").replace("{dept}", department);
  } else if (followUp?.status === "approved") {
    outcome = t("Approved by {dept}", "Freigegeben von {dept}").replace("{dept}", department);
  } else if (
    own.status === "approved" &&
    (followUp?.status === "pending" || result?.resumed === false)
  ) {
    outcome = t(
      "Approved by you · waiting for {dept}",
      "Von dir freigegeben · wartet auf {dept}",
    ).replace("{dept}", department);
  } else if (own.status === "approved") {
    outcome = t("Approved by you", "Von dir freigegeben");
  } else if (own.status === "rejected") {
    outcome = t("Rejected by you", "Von dir abgelehnt");
  } else if (own.status === "expired") {
    outcome = t("Expired", "Abgelaufen");
  }

  return (
    <Card className="space-y-2 border-primary/30 p-3 text-sm">
      <div className="text-xs text-muted-foreground">
        {(approval.agentName || t("The agent", "Der Agent")) + " "}
        {t("wants to", "möchte")}
      </div>
      <div className="font-medium">{action}</div>
      {typeof subject === "string" && subject && (
        <div className="text-sm text-foreground/90">
          {t("Subject", "Betreff")}: {subject}
        </div>
      )}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
        <ShieldCheck aria-hidden className="h-3.5 w-3.5 text-primary" />
        <span>
          {t("Your rule", "Deine Regel")}: {toolLabel(toolKey)} ·{" "}
          {levelLabel(ruleLevel, "member", t)}
        </span>
        {onChangeRule && (
          <button
            type="button"
            onClick={() => onChangeRule(approval.agentId)}
            className="min-h-11 rounded-md px-1 underline-offset-2 hover:text-foreground hover:underline"
          >
            {t("Change rule", "Regel ändern")}
          </button>
        )}
      </div>
      {outcome ? (
        <p aria-live="polite" className="text-sm font-medium">
          {outcome}
        </p>
      ) : (
        <div className="flex flex-wrap gap-2 pt-1">
          <Button
            size="sm"
            className="min-h-11"
            aria-label={`${t("Approve", "Freigeben")}: ${action}`}
            disabled={decide.isPending}
            onClick={() => void decideAs("approve")}
          >
            {t("Approve", "Freigeben")}
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="min-h-11"
            aria-label={`${t("Reject", "Ablehnen")}: ${action}`}
            disabled={decide.isPending}
            onClick={() => void decideAs("reject")}
          >
            {t("Reject", "Ablehnen")}
          </Button>
        </div>
      )}
      {failed && (
        <p role="alert" className="text-xs text-destructive">
          {t(
            "Couldn't record your decision. Try again.",
            "Deine Entscheidung konnte nicht gespeichert werden. Versuch es noch einmal.",
          )}
        </p>
      )}
    </Card>
  );
}
