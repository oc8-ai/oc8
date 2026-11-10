// The pill the agent header shows its status in (StatusPill, app-shell.tsx),
// and the same pill for a run's state -- so the Live Log, the run history and
// the header use one look and one vocabulary: "running" is the same word in
// the same colour wherever it appears.

import { useT } from "@/lib/i18n";
import { RUN_STATE_COLOR } from "@/lib/run-state";
import { cn } from "@/lib/utils";

/** A rounded pill with a glowing dot, tinted by one CSS colour. */
export function TonePill({
  label,
  color,
  className,
  testId,
}: {
  label: string;
  color: string;
  className?: string;
  testId?: string;
}) {
  return (
    <span
      data-testid={testId}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium",
        className,
      )}
      style={{
        color,
        borderColor: `color-mix(in oklab, ${color} 35%, transparent)`,
        background: `color-mix(in oklab, ${color} 10%, transparent)`,
      }}
    >
      <span
        className="h-1.5 w-1.5 rounded-full"
        style={{ background: color, boxShadow: `0 0 8px ${color}` }}
      />
      {label}
    </span>
  );
}

/** Label and colour of a run state. "running" and "waiting" use exactly the
 *  words and colours the header's StatusPill uses for an agent in that state. */
function useRunStateMeta(): (state: string) => { label: string; color: string } {
  const t = useT();
  return (state) => {
    const color = RUN_STATE_COLOR[state] ?? "var(--muted-foreground)";
    switch (state) {
      case "queued":
        return { label: t("queued", "eingereiht"), color };
      case "running":
        return { label: t("running", "läuft"), color };
      case "waiting_for_input":
        return { label: t("waiting for input", "wartet auf Eingabe"), color };
      case "waiting_for_approval":
        return { label: t("waiting for approval", "wartet auf Freigabe"), color };
      case "done":
        return { label: t("done", "erledigt"), color };
      case "failed":
        return { label: t("failed", "fehlgeschlagen"), color };
      case "interrupted":
        return { label: t("interrupted", "unterbrochen"), color };
      default:
        return { label: state, color };
    }
  };
}

export function RunStateBadge({ state, className }: { state: string; className?: string }) {
  const meta = useRunStateMeta()(state);
  return (
    <TonePill
      label={meta.label}
      color={meta.color}
      className={className}
      testId="run-state-badge"
    />
  );
}
