// frontend/src/components/conversation/decision-card.tsx
// request_decision in the chat (spec §4.2). Decided through the same approval
// endpoint as the inbox, so the rules (who may decide) are the inbox's: the
// caller passes `mayDecide` from `mayActOn`, and a member who may not decide
// reads the question without buttons that would only 403.
import { useState } from "react";
import { Check } from "lucide-react";

import { ApiError } from "@/lib/api";
import { useDecideApproval, type Approval } from "@/lib/hooks";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export function DecisionCard({
  approval,
  mayDecide = true,
  onDecided,
}: {
  approval: Approval;
  mayDecide?: boolean;
  /** The decision was saved; the pending list will drop the approval, so the
   *  conversation keeps showing it with the picked option. */
  onDecided?: (option: string) => void;
}) {
  const t = useT();
  const decide = useDecideApproval();
  const [picked, setPicked] = useState<string | null>(approval.decisionOption ?? null);
  // Somebody else decided first (409): nothing left to pick here.
  const [closed, setClosed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const done = picked !== null || closed || approval.status !== "pending";
  const options = approval.options ?? [];

  function pick(key: string) {
    setError(null);
    decide.mutate(
      { approvalId: approval.id, decision: "approve", option: key },
      {
        onSuccess: () => {
          setPicked(key);
          onDecided?.(key);
        },
        onError: (e: unknown) => {
          const status = e instanceof ApiError ? e.status : null;
          if (status === 409) {
            setClosed(true);
            setError(t("Somebody already decided this.", "Das hat schon jemand entschieden."));
          } else if (status === 403) {
            setError(t("You may not decide this.", "Du darfst das nicht entscheiden."));
          } else {
            setError(
              t(
                "The decision was not saved. Try again.",
                "Die Entscheidung wurde nicht gespeichert. Versuch es noch einmal.",
              ),
            );
          }
        },
      },
    );
  }

  const recommended = <span className="text-micro">{t("Recommended", "Empfohlen")}</span>;
  return (
    <section className="space-y-2 rounded-lg border border-border bg-card p-3">
      <div className="flex items-center gap-2" aria-live="polite">
        {!done && <span className="size-2 rounded-full bg-status-warning" aria-hidden />}
        <span className="text-micro">
          {done
            ? t("Decided", "Entschieden")
            : t("Needs your decision", "Braucht deine Entscheidung")}
        </span>
      </div>
      <h3 className="text-base font-semibold">{approval.title}</h3>
      {approval.detail && <p className="text-sm text-muted-foreground">{approval.detail}</p>}
      {mayDecide ? (
        <div className="flex flex-wrap gap-2">
          {options.map((option) => {
            const isRecommended = approval.recommendation === option.key;
            return (
              <button
                key={option.key}
                type="button"
                disabled={done || decide.isPending}
                aria-label={
                  isRecommended
                    ? t(
                        `Decide: ${option.label} (recommended)`,
                        `Entscheiden: ${option.label} (empfohlen)`,
                      )
                    : t(`Decide: ${option.label}`, `Entscheiden: ${option.label}`)
                }
                aria-pressed={picked === option.key}
                onClick={() => pick(option.key)}
                className={cn(
                  "inline-flex min-h-9 items-center gap-2 rounded-md border border-border px-3 text-sm hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-60",
                  picked === option.key && "border-primary text-primary disabled:opacity-100",
                )}
              >
                {picked === option.key && <Check className="size-4" aria-hidden />}
                {option.label}
                {isRecommended && recommended}
              </button>
            );
          })}
        </div>
      ) : (
        <>
          <ul className="flex flex-wrap gap-2">
            {options.map((option) => (
              <li
                key={option.key}
                className="inline-flex min-h-9 items-center gap-2 rounded-md border border-border px-3 text-sm text-muted-foreground"
              >
                {option.label}
                {approval.recommendation === option.key && recommended}
              </li>
            ))}
          </ul>
          <p className="text-xs text-muted-foreground">
            {t(
              "You can read this decision, but you cannot make it. Somebody who approves in this department decides.",
              "Du kannst diese Entscheidung lesen, aber nicht treffen. Das entscheidet jemand, der in dieser Abteilung freigibt.",
            )}
          </p>
        </>
      )}
      {error && (
        <p role="alert" className="text-xs text-status-error">
          {error}
        </p>
      )}
    </section>
  );
}
