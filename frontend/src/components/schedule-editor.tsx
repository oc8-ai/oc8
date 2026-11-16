// An agent's schedule on its page: a preset, or an exact cron built with
// CronBuilder and saved as the agent's one cron trigger, read in the zone
// picked here (a new schedule starts in the organisation's; one saved before
// triggers carried a zone fires in UTC and shows UTC until someone changes it).
import { useEffect, useState } from "react";
import { Save } from "lucide-react";
import { toast } from "sonner";

import { TriggerOwner } from "@/components/connections/trigger-owner";
import { CronBuilder } from "@/components/cron-builder";
import {
  useAgentTriggers,
  useCreateAgentTrigger,
  useDeleteAgentTrigger,
  useUpdateAgentTrigger,
} from "@/lib/hooks";
import { useT } from "@/lib/i18n";
import { scheduleZone, useOrgTimezone } from "@/lib/timezones";
import { cn } from "@/lib/utils";

type TriggerMode = "continuous" | "24-7" | "schedule" | "on-demand" | "custom";

// "schedule" (free-text "Weekly schedule") dropped from the preset list --
// Custom cron already covers everything it could express, so it was a
// redundant option. `detectTriggerMode` below still falls back to it for
// an agent whose `agent.schedule` already holds an old free-text value, so
// that legacy data keeps rendering rather than being reinterpreted as cron.
const TRIGGER_PRESETS: { id: TriggerMode; label: string; example: string }[] = [
  { id: "continuous", label: "Continuous", example: "Continuous" },
  { id: "24-7", label: "24/7", example: "24/7" },
  { id: "on-demand", label: "On-Demand", example: "On-Demand" },
  { id: "custom", label: "Time scheduled", example: "0 */2 * * *" },
];

function detectTriggerMode(value: string): TriggerMode {
  const v = value.trim().toLowerCase();
  if (v === "continuous") return "continuous";
  if (v === "24/7") return "24-7";
  if (v === "on-demand") return "on-demand";
  if (/^[0-9*/,\-\s]+$/.test(v)) return "custom";
  return "schedule";
}

export function ScheduleEditor({
  initial,
  agentName,
  agentId,
}: {
  initial: string;
  agentName: string;
  agentId: string;
}) {
  const t = useT();
  const { data: triggers } = useAgentTriggers(agentId);
  const cronTrigger = triggers?.find((tr) => tr.kind === "cron");
  const createTrigger = useCreateAgentTrigger();
  const updateTrigger = useUpdateAgentTrigger();
  const deleteTrigger = useDeleteAgentTrigger();
  const [mode, setMode] = useState<TriggerMode>(() => detectTriggerMode(initial));
  const [value, setValue] = useState<string>(initial);
  const orgTimezone = useOrgTimezone();
  // The zone the schedule is read in: the saved trigger's (none = UTC), or
  // the organisation's for a new one. `null` until the person picks one.
  const [pickedZone, setPickedZone] = useState<string | null>(null);
  const timezone = pickedZone ?? (cronTrigger ? scheduleZone(cronTrigger.timezone) : orgTimezone);

  useEffect(() => {
    if (cronTrigger?.cronExpression) {
      setMode("custom");
      setValue(cronTrigger.cronExpression);
    }
  }, [cronTrigger?.cronExpression]);

  function pickMode(next: TriggerMode) {
    setMode(next);
    const preset = TRIGGER_PRESETS.find((p) => p.id === next)!;
    if (next !== "schedule" && next !== "custom") {
      setValue(preset.example);
    }
    if (next === "custom") {
      // Seed a valid cron if the current value is not one.
      const isCron = /^[0-9*/,\-\s]+$/.test(value.trim());
      if (!isCron) setValue("0 9 * * 1-5");
    }
  }

  function save() {
    const onFailed = () =>
      toast.error(t("Couldn't update schedule", "Zeitplan konnte nicht aktualisiert werden"), {
        description: t("Try again in a moment.", "Bitte versuche es in Kürze erneut."),
      });
    // Only "custom" mode produces a real cron string (via CronBuilder); the
    // other presets are display-only and never touch the backend.
    if (mode !== "custom") {
      const onDone = () =>
        toast.success(t("Schedule updated", "Zeitplan aktualisiert"), {
          description: `${agentName} · ${value || TRIGGER_PRESETS.find((p) => p.id === mode)?.example || mode}`,
        });
      if (cronTrigger) {
        deleteTrigger.mutate(
          { agentId, triggerId: cronTrigger.id },
          { onSuccess: onDone, onError: onFailed },
        );
      } else {
        onDone();
      }
      return;
    }
    const onSaved = () =>
      toast.success(t("Schedule updated", "Zeitplan aktualisiert"), {
        description: `${agentName} · ${value} (${timezone})`,
      });
    if (cronTrigger) {
      updateTrigger.mutate(
        { agentId, triggerId: cronTrigger.id, cronExpression: value, timezone },
        { onSuccess: onSaved, onError: onFailed },
      );
    } else {
      createTrigger.mutate(
        { agentId, cronExpression: value, timezone, taskText: `Scheduled run for ${agentName}` },
        { onSuccess: onSaved, onError: onFailed },
      );
    }
  }

  const showTextInput = mode === "schedule" || mode === "custom";

  return (
    <div className="mt-4 space-y-3">
      <div className="flex flex-wrap gap-1.5">
        {TRIGGER_PRESETS.map((p) => {
          const active = p.id === mode;
          return (
            <button
              key={p.id}
              type="button"
              onClick={() => pickMode(p.id)}
              className={cn(
                "rounded-full border px-2.5 py-1 text-[11px] transition",
                active
                  ? "border-primary/60 bg-primary/15 text-primary"
                  : "border-border bg-background/40 text-muted-foreground hover:text-foreground",
              )}
            >
              {p.label}
            </button>
          );
        })}
      </div>
      {showTextInput ? (
        mode === "custom" ? (
          <CronBuilder
            initial={value}
            onChange={setValue}
            onSave={save}
            saveLabel={t("Save", "Speichern")}
            timezone={timezone}
            onTimezoneChange={setPickedZone}
          />
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder="z. B. Mon–Fri, 09:00–17:00"
              className="min-w-0 flex-1 rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
            />
            <button
              type="button"
              onClick={save}
              className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90"
            >
              <Save className="h-3.5 w-3.5" /> {t("Save", "Speichern")}
            </button>
          </div>
        )
      ) : (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border bg-background/30 px-3 py-2 text-sm">
          <span className="font-mono text-xs text-muted-foreground">{value}</span>
          <button
            type="button"
            onClick={save}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-xs font-medium text-primary-foreground transition hover:opacity-90"
          >
            <Save className="h-3.5 w-3.5" /> {t("Save", "Speichern")}
          </button>
        </div>
      )}
      <p className="text-[11px] text-muted-foreground">
        {t(
          "Pick a preset or define an exact schedule / cron expression.",
          "Wähle einen Preset oder definiere einen exakten Zeitplan / Cron-Ausdruck.",
        )}
      </p>
      {cronTrigger && <TriggerOwner agentId={agentId} trigger={cronTrigger} />}
    </div>
  );
}
