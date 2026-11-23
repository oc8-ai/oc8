import { Link } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { ApiError } from "@/lib/api";
import { useMay } from "@/lib/governance-hooks";
import {
  useAgents,
  useHirePersonalTemplate,
  useMyPersonalDepartment,
  usePersonalTemplates,
} from "@/lib/hooks";
import { useT } from "@/lib/i18n";

/** The machine code of a refused request. FastAPI wraps a dict `detail` as
 *  `{detail: {error}}`; `toError` in `lib/api.ts` keeps that body on the error. */
function errorCode(err: unknown): string | null {
  if (!(err instanceof ApiError)) return null;
  const body = err.body as { detail?: unknown; error?: unknown } | undefined;
  const detail = body?.detail;
  if (detail !== null && typeof detail === "object") {
    const code = (detail as { error?: unknown }).error;
    if (typeof code === "string") return code;
  }
  return typeof body?.error === "string" ? body.error : null;
}

function MyAgents({ departmentId }: { departmentId: string }) {
  const t = useT();
  const { data } = useAgents({ filters: { departmentId } });
  const items = Array.isArray(data?.items) ? data.items : [];
  if (items.length === 0) {
    return (
      <p className="mt-3 text-sm text-muted-foreground">
        {t("You have no personal assistants yet.", "Du hast noch keine persönlichen Assistenten.")}
      </p>
    );
  }
  return (
    <ul className="mt-3 space-y-1">
      {items.map((a) => (
        <li key={a.id}>
          <Link
            to="/agents/$id"
            params={{ id: a.id }}
            className="text-sm font-medium text-foreground hover:underline"
          >
            {a.name}
          </Link>
        </li>
      ))}
    </ul>
  );
}

/** Profile: the member's own personal department -- their agents, and the
 *  personal templates they may hire. */
export function PersonalAssistantsPanel() {
  const t = useT();
  const may = useMay();
  const allowed = may("department:own");
  const mine = useMyPersonalDepartment(allowed);
  const templates = usePersonalTemplates(allowed);
  const hire = useHirePersonalTemplate();

  if (!allowed || !mine.data) return null;
  const { enabled, department } = mine.data;
  // Nothing to show: switched off and nothing hired before.
  if (!enabled && !department) return null;

  const hireErrorText = (err: unknown): string => {
    const code = errorCode(err);
    if (code === "personal_agent_limit")
      return t(
        "You've reached the maximum number of personal assistants",
        "Du hast die maximale Anzahl persönlicher Assistenten erreicht",
      );
    if (code === "personal_departments_disabled")
      return t("Personal assistants are turned off", "Persönliche Assistenten sind ausgeschaltet");
    if (code === "template_not_personal" || (err instanceof ApiError && err.status === 404))
      return t("This assistant is not available", "Dieser Assistent ist nicht verfügbar");
    if (code === "owner_left")
      return t(
        "Your account can no longer hire assistants",
        "Dein Konto kann keine Assistenten mehr einstellen",
      );
    if (err instanceof ApiError && err.status === 400)
      return t(
        "This assistant isn't allowed by your organisation's rules",
        "Dieser Assistent ist nach den Regeln deiner Organisation nicht erlaubt",
      );
    return t("Couldn't hire the assistant", "Assistent konnte nicht eingestellt werden");
  };

  const list = Array.isArray(templates.data) ? templates.data : [];
  const hiringId = hire.isPending ? hire.variables?.capaId : null;

  return (
    <Panel className="p-4">
      <h2 className="text-sm font-medium">
        {t("My personal assistants", "Meine persönlichen Assistenten")}
      </h2>
      {department && <MyAgents departmentId={department.id} />}
      {!enabled ? (
        <p className="mt-3 text-sm text-muted-foreground">
          {t(
            "New hires are turned off by your organisation.",
            "Neue Einstellungen sind von deiner Organisation ausgeschaltet.",
          )}
        </p>
      ) : (
        <div className="mt-4 border-t border-border pt-4">
          <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {t("Available assistants", "Verfügbare Assistenten")}
          </h3>
          {list.length === 0 ? (
            <p className="mt-2 text-sm text-muted-foreground">
              {t(
                "No personal assistants are available yet.",
                "Es sind noch keine persönlichen Assistenten verfügbar.",
              )}
            </p>
          ) : (
            <ul className="mt-2 space-y-2">
              {list.map((tpl) => (
                <li key={tpl.capaId} className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="text-sm font-medium">{tpl.label}</div>
                    {tpl.summary && (
                      <div className="text-xs text-muted-foreground">{tpl.summary}</div>
                    )}
                  </div>
                  <button
                    type="button"
                    disabled={hire.isPending}
                    onClick={() =>
                      hire.mutate(
                        { capaId: tpl.capaId },
                        {
                          onSuccess: () =>
                            toast.success(t("Assistant hired", "Assistent eingestellt")),
                          onError: (err) => toast.error(hireErrorText(err)),
                        },
                      )
                    }
                    className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-md bg-primary px-3 text-xs font-medium text-primary-foreground disabled:opacity-50"
                  >
                    {hiringId === tpl.capaId && <Loader2 className="h-3 w-3 animate-spin" />}
                    {t("Hire", "Einstellen")}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </Panel>
  );
}
