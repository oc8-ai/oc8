import { Link } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { PersonalConfirmationNotice } from "@/components/personal-confirmation-notice";
import { PersonalHireDialog } from "@/components/personal-hire-dialog";
import { useMay } from "@/lib/governance-hooks";
import {
  useAgents,
  useHirePersonalTemplate,
  useMyPersonalDepartment,
  usePersonalTemplates,
} from "@/lib/hooks";
import { useT } from "@/lib/i18n";
import { hireErrorText, needsHireDialog } from "@/lib/personal-hire";

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
  const templates = usePersonalTemplates(allowed && !!mine.data?.enabled);
  const hire = useHirePersonalTemplate();
  const [dialogFor, setDialogFor] = useState<string | null>(null);

  if (!allowed || !mine.data) return null;
  const { enabled, department } = mine.data;
  // Nothing to show: switched off and nothing hired before.
  if (!enabled && !department) return null;

  const list = Array.isArray(templates.data) ? templates.data : [];
  const dialogTemplate = list.find((tpl) => tpl.capaId === dialogFor);
  const hiringId = hire.isPending ? hire.variables?.capaId : null;

  return (
    <Panel className="p-4">
      <h2 className="text-sm font-medium">
        {t("My personal assistants", "Meine persönlichen Assistenten")}
      </h2>
      {department && <PersonalConfirmationNotice enabled />}
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
                      needsHireDialog(tpl)
                        ? setDialogFor(tpl.capaId)
                        : hire.mutate(
                            { capaId: tpl.capaId, hireToken: tpl.hireToken },
                            {
                              onSuccess: () =>
                                toast.success(t("Assistant hired", "Assistent eingestellt")),
                              onError: (err) => toast.error(hireErrorText(err, t)),
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
      {dialogTemplate && (
        <PersonalHireDialog
          template={dialogTemplate}
          open
          onOpenChange={(o) => {
            if (!o) setDialogFor(null);
          }}
        />
      )}
    </Panel>
  );
}
