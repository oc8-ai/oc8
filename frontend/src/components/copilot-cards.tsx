import type { ReactElement } from "react";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { useT } from "@/lib/i18n";

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

function formatWhen(value: string, timeZone: string | null): string {
  if (!value) return "";
  try {
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return value;
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
      timeZone: timeZone ?? undefined,
    }).format(d);
  } catch {
    return value;
  }
}

export function ResponsibilityCard({ props }: { props: Record<string, unknown> }): ReactElement {
  const t = useT();
  const title = str(props.title);
  const goal = str(props.goal);
  const state = str(props.state);
  const nextStep = str(props.nextStep);
  const labels: Record<string, string> = {
    active: t("active", "aktiv"),
    waiting: t("waiting", "wartet"),
    paused: t("paused", "pausiert"),
    done: t("done", "erledigt"),
    cancelled: t("cancelled", "abgebrochen"),
  };
  return (
    <Card className="space-y-1.5 p-3 text-sm">
      <div className="flex items-start justify-between gap-2">
        <div className="font-medium">{title}</div>
        {state && <Badge variant="secondary">{labels[state] ?? state}</Badge>}
      </div>
      {goal && <p className="text-xs text-muted-foreground">{goal}</p>}
      {nextStep && (
        <p className="text-xs">
          <span className="text-muted-foreground">{t("Next step", "Nächster Schritt")}: </span>
          {nextStep}
        </p>
      )}
    </Card>
  );
}

export function FollowupCard({ props }: { props: Record<string, unknown> }): ReactElement {
  const t = useT();
  const title = str(props.responsibilityTitle);
  const recurring = props.kind === "cron";
  const timezone = typeof props.timezone === "string" && props.timezone ? props.timezone : null;
  const when = str(props.when);
  const endsAt = str(props.endsAt);
  return (
    <Card className="space-y-1.5 p-3 text-sm">
      <div className="flex items-start justify-between gap-2">
        <div className="font-medium">{title}</div>
        <div className="flex shrink-0 gap-1">
          {props.purpose === "research" && (
            <Badge variant="secondary">{t("Research · read-only", "Recherche · nur lesend")}</Badge>
          )}
          <Badge variant="outline">
            {recurring ? t("recurring", "wiederkehrend") : t("once", "einmalig")}
          </Badge>
        </div>
      </div>
      {when && (
        <p className="text-xs">
          <span className="text-muted-foreground">{t("Next", "Nächste")}: </span>
          {formatWhen(when, timezone)}
        </p>
      )}
      {timezone && <p className="text-xs text-muted-foreground">{timezone}</p>}
      {endsAt && (
        <p className="text-xs" data-testid="followup-ends">
          <span className="text-muted-foreground">{t("Ends", "Endet")}: </span>
          {formatWhen(endsAt, timezone)}
        </p>
      )}
    </Card>
  );
}
