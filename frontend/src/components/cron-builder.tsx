import { useState } from "react";
import { Save } from "lucide-react";
import { describeCron } from "@/lib/describe-cron";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

// Extracted from agents.$id.tsx's ScheduleEditor so the Hire dialog can offer
// the same real cron picker at creation time instead of a dead "Schedule"
// button that saves nothing.

type CronFreq = "every-n-min" | "hourly" | "daily" | "weekly" | "monthly";

interface CronState {
  freq: CronFreq;
  everyNMin: number;
  minute: number;
  hour: number;
  weekdays: number[]; // 0=Sun … 6=Sat
  dayOfMonth: number;
}

const DEFAULT_CRON: CronState = {
  freq: "daily",
  everyNMin: 15,
  minute: 0,
  hour: 9,
  weekdays: [1, 2, 3, 4, 5],
  dayOfMonth: 1,
};

export function buildCron(s: CronState): string {
  switch (s.freq) {
    case "every-n-min":
      return `*/${Math.max(1, Math.min(59, s.everyNMin))} * * * *`;
    case "hourly":
      return `${s.minute} * * * *`;
    case "daily":
      return `${s.minute} ${s.hour} * * *`;
    case "weekly": {
      const days = s.weekdays.length ? [...s.weekdays].sort().join(",") : "*";
      return `${s.minute} ${s.hour} * * ${days}`;
    }
    case "monthly":
      return `${s.minute} ${s.hour} ${s.dayOfMonth} * *`;
  }
}

export function parseCron(expr: string): CronState {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return DEFAULT_CRON;
  const [m, h, dom, , dow] = parts;
  const num = (v: string, fb: number) => (/^\d+$/.test(v) ? Number(v) : fb);
  if (m.startsWith("*/")) {
    return { ...DEFAULT_CRON, freq: "every-n-min", everyNMin: num(m.slice(2), 15) };
  }
  if (h === "*" && dom === "*" && dow === "*") {
    return { ...DEFAULT_CRON, freq: "hourly", minute: num(m, 0) };
  }
  if (dom === "*" && dow !== "*") {
    const weekdays = dow
      .split(",")
      .map((x) => Number(x))
      .filter((n) => Number.isFinite(n) && n >= 0 && n <= 6);
    return {
      ...DEFAULT_CRON,
      freq: "weekly",
      minute: num(m, 0),
      hour: num(h, 9),
      weekdays: weekdays.length ? weekdays : DEFAULT_CRON.weekdays,
    };
  }
  if (dom !== "*" && dow === "*") {
    return {
      ...DEFAULT_CRON,
      freq: "monthly",
      minute: num(m, 0),
      hour: num(h, 9),
      dayOfMonth: num(dom, 1),
    };
  }
  return { ...DEFAULT_CRON, freq: "daily", minute: num(m, 0), hour: num(h, 9) };
}

const WEEKDAY_LABELS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];

export function CronBuilder({
  initial,
  onChange,
  onSave,
  saveLabel,
}: {
  initial: string;
  onChange: (cron: string) => void;
  onSave: () => void;
  saveLabel: string;
}) {
  const t = useT();
  const [state, setState] = useState<CronState>(() => parseCron(initial));

  function update(patch: Partial<CronState>) {
    setState((prev) => {
      const next = { ...prev, ...patch };
      onChange(buildCron(next));
      return next;
    });
  }

  const cron = buildCron(state);
  const human = describeCron(cron, t);

  return (
    <div className="space-y-3 rounded-md border border-border bg-background/30 p-3">
      <div className="grid gap-2 sm:grid-cols-[minmax(0,180px)_minmax(0,1fr)]">
        <label className="block">
          <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
            Frequency
          </span>
          <select
            value={state.freq}
            onChange={(e) => update({ freq: e.target.value as CronFreq })}
            className="mt-1 w-full rounded-md border border-border bg-background/40 px-2 py-1.5 text-sm outline-none focus:border-primary/50"
          >
            <option value="every-n-min">Every N minutes</option>
            <option value="hourly">Hourly</option>
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
            <option value="monthly">Monthly</option>
          </select>
        </label>

        <div>
          {state.freq === "every-n-min" && (
            <NumField
              label="Every N minutes"
              min={1}
              max={59}
              value={state.everyNMin}
              onChange={(v) => update({ everyNMin: v })}
            />
          )}
          {state.freq === "hourly" && (
            <NumField
              label="At minute"
              min={0}
              max={59}
              value={state.minute}
              onChange={(v) => update({ minute: v })}
            />
          )}
          {(state.freq === "daily" || state.freq === "weekly" || state.freq === "monthly") && (
            <div className="grid grid-cols-2 gap-2">
              <NumField
                label="Hour (24h)"
                min={0}
                max={23}
                value={state.hour}
                onChange={(v) => update({ hour: v })}
              />
              <NumField
                label="Minute"
                min={0}
                max={59}
                value={state.minute}
                onChange={(v) => update({ minute: v })}
              />
            </div>
          )}
        </div>
      </div>

      {state.freq === "weekly" && (
        <div>
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
            Weekdays
          </div>
          <div className="mt-1 flex flex-wrap gap-1.5">
            {WEEKDAY_LABELS.map((lbl, idx) => {
              const active = state.weekdays.includes(idx);
              return (
                <button
                  key={idx}
                  type="button"
                  onClick={() =>
                    update({
                      weekdays: active
                        ? state.weekdays.filter((d) => d !== idx)
                        : [...state.weekdays, idx],
                    })
                  }
                  className={cn(
                    "h-7 min-w-8 rounded-md border px-2 text-[11px] transition",
                    active
                      ? "border-primary/60 bg-primary/15 text-primary"
                      : "border-border bg-background/40 text-muted-foreground hover:text-foreground",
                  )}
                >
                  {lbl}
                </button>
              );
            })}
          </div>
        </div>
      )}

      {state.freq === "monthly" && (
        <NumField
          label="Day of month"
          min={1}
          max={31}
          value={state.dayOfMonth}
          onChange={(v) => update({ dayOfMonth: v })}
        />
      )}

      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-3">
        <div className="min-w-0">
          <div className="text-[11px] text-foreground/90">{human}</div>
          <code className="font-mono text-[11px] text-muted-foreground">{cron}</code>
        </div>
        <button
          type="button"
          onClick={onSave}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90"
        >
          <Save className="h-3.5 w-3.5" /> {saveLabel}
        </button>
      </div>
    </div>
  );
}

function NumField({
  label,
  min,
  max,
  value,
  onChange,
}: {
  label: string;
  min: number;
  max: number;
  value: number;
  onChange: (v: number) => void;
}) {
  return (
    <label className="block">
      <span className="text-[10px] uppercase tracking-widest text-muted-foreground">{label}</span>
      <input
        type="number"
        min={min}
        max={max}
        value={value}
        onChange={(e) => {
          const raw = Number(e.target.value);
          if (!Number.isFinite(raw)) return;
          onChange(Math.max(min, Math.min(max, raw)));
        }}
        className="mt-1 w-full rounded-md border border-border bg-background/40 px-2 py-1.5 text-sm outline-none focus:border-primary/50"
      />
    </label>
  );
}
