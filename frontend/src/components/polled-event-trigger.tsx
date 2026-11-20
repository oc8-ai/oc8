// A trigger on a polled event (`oc8.events.sources`), e.g. "On new email": core
// polls an outside system through the agent's own fixed login for the event's
// tools and starts the agent for each new thing. The login is shown, never
// picked here -- it is the agent's tool setting. The settings are the ones the
// event declares (`TriggerDecl.config`), the task text gets the event appended
// as data, and the poll status shows when it last worked and what failed.
import { useEffect, useState } from "react";
import { Link } from "@tanstack/react-router";
import { Save, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { TriggerOwner } from "@/components/connections/trigger-owner";
import { useAgentToolAccess } from "@/lib/connections";
import {
  EVENT_TARGET_CHANGED,
  type TriggerDTO,
  type TriggerEventDTO,
  useCreateAgentTrigger,
  useDeleteAgentTrigger,
  useUpdateAgentTrigger,
} from "@/lib/hooks";
import { type Lang, resolveTranslation, useLang, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

type Translate = (en: string, de?: string) => string;

/** Each event's tab name: its `editorLabel` (else its label), translated by
 *  its capa; two equal names get their capa's name in parentheses. */
export function polledEventTabLabels(
  events: TriggerEventDTO[],
  _t: Translate,
  lang: Lang,
): string[] {
  const names = events.map((event) =>
    event.editorLabel
      ? resolveTranslation(event.editorLabel, event.editorLabelTranslations, lang)
      : resolveTranslation(event.label, event.labelTranslations, lang),
  );
  return names.map((name, i) =>
    names.filter((other) => other === name).length > 1
      ? `${name} (${resolveTranslation(events[i].sourceLabel, events[i].sourceLabelTranslations, lang)})`
      : name,
  );
}

function formatWhen(iso: string, lang: Lang): string {
  return new Date(iso).toLocaleString(lang === "de" ? "de-DE" : "en-US", {
    dateStyle: "short",
    timeStyle: "short",
  });
}

function initialConfig(event: TriggerEventDTO, trigger: TriggerDTO | undefined) {
  return Object.fromEntries(
    event.config.map((field) => [field.key, trigger?.eventConfig?.[field.key] ?? field.default]),
  );
}

export function PolledEventTriggerPanel({
  agentId,
  agentName,
  event,
  trigger,
}: {
  agentId: string;
  agentName: string;
  event: TriggerEventDTO;
  trigger: TriggerDTO | undefined;
}) {
  const t = useT();
  const { lang } = useLang();
  const { data: access } = useAgentToolAccess(agentId);
  const createTrigger = useCreateAgentTrigger();
  const updateTrigger = useUpdateAgentTrigger();
  const deleteTrigger = useDeleteAgentTrigger();
  const login = access?.find(
    (row) => row.toolKey === event.toolKey && row.mode === "fixed" && !!row.credentialId,
  );
  const [config, setConfig] = useState<Record<string, string>>(() => initialConfig(event, trigger));
  const [taskText, setTaskText] = useState(
    () =>
      trigger?.taskText ??
      t("Read the new email and take care of it.", "Lies die neue E-Mail und kümmere dich darum."),
  );

  useEffect(() => {
    setConfig(initialConfig(event, trigger));
    if (trigger) setTaskText(trigger.taskText);
    // Only when another trigger is loaded; typing must not be reset.
  }, [trigger?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const onFailed = (error: unknown) =>
    toast.error(t("Couldn't save the trigger", "Auslöser konnte nicht gespeichert werden"), {
      description: error instanceof Error ? error.message : undefined,
    });
  const onSaved = () =>
    toast.success(t("Trigger saved", "Auslöser gespeichert"), { description: agentName });

  function save() {
    if (!login?.credentialId) return;
    if (trigger) {
      updateTrigger.mutate(
        {
          agentId,
          triggerId: trigger.id,
          eventConfig: config,
          taskText,
          eventTargetCredentialId: login.credentialId,
        },
        { onSuccess: onSaved, onError: onFailed },
      );
    } else {
      createTrigger.mutate(
        {
          agentId,
          kind: "event",
          eventSource: event.source,
          eventType: event.type,
          eventConfig: config,
          taskText,
        },
        { onSuccess: onSaved, onError: onFailed },
      );
    }
  }

  function setEnabled(enabled: boolean) {
    if (!trigger) return;
    updateTrigger.mutate(
      enabled && login?.credentialId
        ? { agentId, triggerId: trigger.id, enabled, eventTargetCredentialId: login.credentialId }
        : { agentId, triggerId: trigger.id, enabled },
      { onError: onFailed },
    );
  }

  function remove() {
    if (!trigger) return;
    deleteTrigger.mutate(
      { agentId, triggerId: trigger.id },
      {
        onSuccess: () => toast.success(t("Trigger removed", "Auslöser entfernt")),
        onError: onFailed,
      },
    );
  }

  const toolSettings = (
    <Link
      to="/agents/$id"
      params={{ id: agentId }}
      search={{ tab: "guardrails" }}
      className="underline"
    >
      {t("Open the tool settings", "Tool-Einstellungen öffnen")}
    </Link>
  );

  return (
    <div className="mt-4 space-y-3">
      {login ? (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border bg-background/30 px-3 py-2 text-sm">
          <div className="min-w-0">
            <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
              {t("Mailbox", "Postfach")}
            </div>
            <div className="truncate font-medium">{login.targetLabel ?? login.label}</div>
          </div>
          <span className="text-[11px] text-muted-foreground">{toolSettings}</span>
        </div>
      ) : (
        <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm">
          <p className="font-medium">
            {t("Give this agent a mailbox first", "Gib diesem Agenten zuerst ein Postfach")}
          </p>
          <p className="mt-1 text-[11px] text-muted-foreground">
            {t(
              "It listens on the mailbox the agent uses for its tools. ",
              "Er hört auf das Postfach, das der Agent für seine Tools nutzt. ",
            )}
            {toolSettings}
          </p>
        </div>
      )}

      {login && (
        <>
          {event.config.map((field) => (
            <label key={field.key} className="block text-sm">
              <span className="text-[11px] text-muted-foreground">
                {resolveTranslation(field.label, field.labelTranslations, lang)}
              </span>
              <input
                value={config[field.key] ?? ""}
                onChange={(e) => setConfig((c) => ({ ...c, [field.key]: e.target.value }))}
                maxLength={200}
                placeholder={
                  field.defaultFrom
                    ? t("From the connection's setup", "Aus der Einrichtung der Verbindung")
                    : field.default
                }
                className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
              />
            </label>
          ))}
          <label className="block text-sm">
            <span className="text-[11px] text-muted-foreground">{t("Task", "Aufgabe")}</span>
            <textarea
              value={taskText}
              onChange={(e) => setTaskText(e.target.value)}
              rows={3}
              className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
            />
          </label>
          <p className="text-[11px] text-muted-foreground">
            {t(
              "The email is appended to this task as data from outside, never as instructions.",
              "Die E-Mail wird an diese Aufgabe als Daten von außen angehängt, nie als Anweisung.",
            )}
          </p>
          <button
            type="button"
            onClick={save}
            disabled={!taskText.trim() || createTrigger.isPending || updateTrigger.isPending}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Save className="h-3.5 w-3.5" /> {t("Save", "Speichern")}
          </button>
        </>
      )}

      {trigger && (
        <>
          <label className="flex min-h-11 items-center gap-2 text-sm">
            <button
              type="button"
              role="switch"
              aria-checked={trigger.enabled}
              aria-label={t("Listening", "Hört zu")}
              disabled={updateTrigger.isPending || (!trigger.enabled && !login)}
              onClick={() => setEnabled(!trigger.enabled)}
              className={cn(
                "relative h-5 w-9 shrink-0 rounded-full border transition disabled:opacity-50",
                trigger.enabled ? "border-primary bg-primary/40" : "border-border bg-background/40",
              )}
            >
              <span
                className={cn(
                  "absolute top-0.5 h-3.5 w-3.5 rounded-full transition-all",
                  trigger.enabled ? "left-4 bg-primary" : "left-0.5 bg-muted-foreground",
                )}
              />
            </button>
            {trigger.enabled ? t("On", "An") : t("Off", "Aus")}
          </label>
          <PollStatusLine trigger={trigger} />
          <button
            type="button"
            onClick={remove}
            disabled={deleteTrigger.isPending}
            className="inline-flex items-center gap-1 text-[11px] text-muted-foreground transition hover:text-foreground disabled:opacity-50"
          >
            <Trash2 className="h-3 w-3" />
            {t("Remove trigger", "Auslöser entfernen")}
          </button>
          <TriggerOwner agentId={agentId} trigger={trigger} />
        </>
      )}
    </div>
  );
}

/** When polling last ran and worked, and the error that is still current. */
function PollStatusLine({ trigger }: { trigger: TriggerDTO }) {
  const t = useT();
  const { lang } = useLang();
  const status = trigger.pollStatus;
  const changed = !trigger.enabled && trigger.lastSkipReason === EVENT_TARGET_CHANGED;
  const failing =
    !!status?.lastError &&
    !!status.lastErrorAt &&
    (!status.lastOkAt || Date.parse(status.lastErrorAt) > Date.parse(status.lastOkAt));
  return (
    <div className="space-y-1.5 text-[11px] text-muted-foreground">
      <p role="status">
        {changed
          ? t(
              "Mailbox changed: switched off. Switch it on to listen on the agent's current mailbox.",
              "Postfach geändert: ausgeschaltet. Schalte ihn ein, um auf das aktuelle Postfach des Agenten zu hören.",
            )
          : status?.lastPolledAt
            ? t("Last checked {time}", "Zuletzt geprüft {time}").replace(
                "{time}",
                formatWhen(status.lastPolledAt, lang),
              )
            : t("Not checked yet", "Noch nicht geprüft")}
      </p>
      {failing && status?.lastErrorAt && (
        <p
          role="alert"
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-foreground"
        >
          {t("Error at {time}: {error}", "Fehler um {time}: {error}")
            .replace("{time}", formatWhen(status.lastErrorAt, lang))
            .replace("{error}", status.lastError ?? "")}{" "}
          {status.lastOkAt
            ? t("Last success {time}.", "Zuletzt erfolgreich {time}.").replace(
                "{time}",
                formatWhen(status.lastOkAt, lang),
              )
            : t("It has not worked yet.", "Es hat noch nicht funktioniert.")}
        </p>
      )}
    </div>
  );
}
