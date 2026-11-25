import { toast } from "sonner";
import { type TriggerDTO, useAuth, useUpdateAgentTrigger } from "@/lib/hooks";
import { useT } from "@/lib/i18n";
import { SCHEDULE_CHANGED } from "./texts";

/** Whose trigger this is, and (webhook and event triggers, which anyone who
 *  reaches them can fire) whether its runs act for that person. Only the
 *  creator may switch that on or off; the server refuses everybody else. */
export function TriggerOwner({ agentId, trigger }: { agentId: string; trigger: TriggerDTO }) {
  const t = useT();
  const { data: me } = useAuth();
  const update = useUpdateAgentTrigger();
  const owner = trigger.createdByMemberId;
  const ownerName = trigger.createdByName || t("a former member", "ein ehemaliges Mitglied");
  const isCreator = !!owner && !!me?.memberId && me.memberId === owner;
  const offersActing = trigger.kind === "webhook" || trigger.kind === "event";

  const onError = () =>
    toast.error(
      t(
        "The setting could not be saved. Please try again.",
        "Die Einstellung konnte nicht gespeichert werden. Bitte versuch es erneut.",
      ),
    );
  const toggle = (next: boolean) =>
    update.mutate({ agentId, triggerId: trigger.id, actsForCreator: next }, { onError });
  // Its creator saving it again confirms the agent as it is now (the server
  // sets `needsConfirmation` for the creator only).
  const confirm = () =>
    update.mutate(
      trigger.kind === "cron" && trigger.cronExpression
        ? { agentId, triggerId: trigger.id, cronExpression: trigger.cronExpression }
        : { agentId, triggerId: trigger.id, actsForCreator: trigger.actsForCreator },
      { onError },
    );

  // In a department with an owner only the owner's confirmation card
  // confirms what changed (a save there confirms nothing it did not hold
  // before), so the trigger points to the card instead.
  const viaCard = !!trigger.ownerConfirms || trigger.lastSkipReason === "needs_confirmation";
  const askToSaveAgain = trigger.needsConfirmation && isCreator && !viaCard;
  const pointToCard = trigger.needsConfirmation && isCreator && !!trigger.ownerConfirms;

  let hint: string | null = null;
  if (offersActing && !owner) {
    hint =
      trigger.kind === "webhook"
        ? t("This webhook has no owner.", "Dieser Webhook hat keinen Besitzer.")
        : t("This trigger has no owner.", "Dieser Auslöser hat keinen Besitzer.");
  } else if (offersActing && !isCreator) {
    hint = t(`Only ${ownerName} can change this.`, `Nur ${ownerName} kann das ändern.`);
  }

  return (
    <div className="space-y-1.5 text-[11px] text-muted-foreground">
      {askToSaveAgain && (
        <div
          role="status"
          className="flex flex-wrap items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-foreground"
        >
          <span>{t(SCHEDULE_CHANGED.en, SCHEDULE_CHANGED.de)}</span>
          <button
            type="button"
            onClick={confirm}
            disabled={update.isPending}
            className="min-h-11 font-medium underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-60"
          >
            {t("Save the schedule again", "Zeitplan erneut speichern")}
          </button>
        </div>
      )}
      {pointToCard && (
        <p
          role="status"
          className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-foreground"
        >
          {t(
            "The agent was changed. Please review and confirm the changes in your profile, under your personal assistants.",
            "Der Agent wurde geändert. Bitte prüfe und bestätige die Änderungen in deinem Profil, bei deinen persönlichen Assistenten.",
          )}
        </p>
      )}
      {owner ? (
        <p>{t(`Created by ${ownerName}`, `Angelegt von ${ownerName}`)}</p>
      ) : (
        trigger.kind === "cron" && (
          <p>
            {t(
              "This schedule has no owner. Please save it once again.",
              "Dieser Zeitplan hat keinen Besitzer. Bitte speicher ihn einmal neu.",
            )}
          </p>
        )
      )}
      {offersActing && (
        <>
          <label className="flex min-h-11 items-center gap-2 text-sm text-foreground">
            <input
              type="checkbox"
              checked={trigger.actsForCreator}
              disabled={!isCreator || update.isPending}
              onChange={(event) => toggle(event.target.checked)}
              className="h-4 w-4 accent-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-60"
            />
            {t("Run on behalf of the creator", "Im Namen des Erstellers ausführen")}
          </label>
          <p>
            {t(
              "Its runs then use the creator's personal accounts. Anyone who can reach this trigger can start them.",
              "Die Läufe nutzen dann die persönlichen Konten der Person, die den Auslöser angelegt hat. Wer den Auslöser erreicht, kann sie starten.",
            )}
          </p>
          {hint && <p>{hint}</p>}
        </>
      )}
    </div>
  );
}
