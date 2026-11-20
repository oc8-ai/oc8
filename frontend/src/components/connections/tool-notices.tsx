import { Info } from "lucide-react";
import type { ToolNoticeInfo } from "@/lib/connections";
import { useT } from "@/lib/i18n";

/** Tools a run could not offer because of an admin's configuration error
 *  (an incomplete login, several logins and none chosen): a quiet info line
 *  each, next to the connect call to action. Not an alert -- the run went
 *  on without the tool. */
export function ToolNotices({ value }: { value?: ToolNoticeInfo[] | null }) {
  const t = useT();
  if (!value?.length) return null;
  return (
    <div className="space-y-1">
      {value.map((notice) => (
        <p
          key={notice.tool}
          role="status"
          className="flex items-start gap-1.5 text-xs text-muted-foreground"
        >
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
          <span>{noticeText(t, notice)}</span>
        </p>
      ))}
    </div>
  );
}

function noticeText(t: ReturnType<typeof useT>, notice: ToolNoticeInfo): string {
  const head = t(
    `${notice.label} was not available in this run: `,
    `${notice.label} war in diesem Lauf nicht verfügbar: `,
  );
  if (notice.reason === "incomplete") {
    const fields = notice.missing.join(", ");
    return (
      head +
      t(
        `the login is incomplete (${fields} missing). Ask an admin to complete it under Settings → Connections.`,
        `Der Zugang ist unvollständig (${fields} fehlt). Bitte eine Admin-Person, ihn unter Einstellungen → Verbindungen zu vervollständigen.`,
      )
    );
  }
  return (
    head +
    t(
      "several logins are enabled for this department and the agent has none chosen.",
      "Für diese Abteilung sind mehrere Zugänge freigegeben und der Agent hat keinen gewählt.",
    )
  );
}
