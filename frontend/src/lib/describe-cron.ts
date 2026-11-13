// A five-field cron expression in plain words, for every place that shows a
// schedule to a person (the cron picker, the agent builder's card). Strict on
// purpose: a pattern it does not recognise reads as a custom schedule with the
// expression itself, never as a guess -- "every day at 9:00" for a cron that
// runs on the 1st of each quarter would be worse than no description.
import type { Translate } from "@/lib/tool-labels";

const DAYS_EN = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const DAYS_DE = ["Sonntag", "Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag"];

function int(field: string, min: number, max: number): number | null {
  if (!/^\d+$/.test(field)) return null;
  const n = Number(field);
  return n >= min && n <= max ? n : null;
}

function step(field: string, max: number): number | null {
  const match = /^\*\/(\d+)$/.exec(field);
  if (!match) return null;
  const n = Number(match[1]);
  return n >= 1 && n <= max ? n : null;
}

/** "1-5", "1,3,5", "0" -> sorted unique weekdays 0..6 (7 is Sunday too). */
function weekdays(field: string): number[] | null {
  const out = new Set<number>();
  for (const part of field.split(",")) {
    const range = /^(\d)-(\d)$/.exec(part);
    if (range) {
      const from = Number(range[1]);
      const to = Number(range[2]);
      if (from > to || to > 7) return null;
      for (let d = from; d <= to; d++) out.add(d % 7);
      continue;
    }
    const day = int(part, 0, 7);
    if (day === null) return null;
    out.add(day % 7);
  }
  return out.size ? [...out].sort((a, b) => a - b) : null;
}

function list(items: string[], and: string): string {
  if (items.length < 2) return items.join("");
  return `${items.slice(0, -1).join(", ")} ${and} ${items[items.length - 1]}`;
}

function pad2(n: number): string {
  return n.toString().padStart(2, "0");
}

/**
 * `timeZone`: the zone the schedule fires in, named after a time of day
 * ("Every day at 8:00 UTC") -- the scheduler evaluates crons in UTC, and a
 * reader would otherwise assume their own clock.
 */
export function describeCron(expr: string, t: Translate, timeZone?: string): string {
  const custom = t(`Custom schedule (${expr.trim()})`, `Benutzerdefiniert (${expr.trim()})`);
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return custom;
  const [min, hour, dom, month, dow] = parts;
  if (month !== "*") return custom;

  const everyMinutes = step(min, 59);
  if (everyMinutes !== null && hour === "*" && dom === "*" && dow === "*") {
    return everyMinutes === 1
      ? t("Every minute", "Jede Minute")
      : t(`Every ${everyMinutes} minutes`, `Alle ${everyMinutes} Minuten`);
  }

  const minute = int(min, 0, 59);
  if (minute === null) return custom;

  const everyHours = step(hour, 23);
  if (dom === "*" && dow === "*" && (hour === "*" || everyHours !== null)) {
    const at = `:${pad2(minute)}`;
    if (hour === "*" || everyHours === 1) {
      return minute === 0
        ? t("Every hour", "Jede Stunde")
        : t(`Every hour at ${at}`, `Jede Stunde um ${at}`);
    }
    return minute === 0
      ? t(`Every ${everyHours} hours`, `Alle ${everyHours} Stunden`)
      : t(`Every ${everyHours} hours at ${at}`, `Alle ${everyHours} Stunden um ${at}`);
  }

  const h = int(hour, 0, 23);
  if (h === null) return custom;
  const zone = timeZone ? ` ${timeZone}` : "";
  const time = `${h}:${pad2(minute)}${zone}`;
  // German puts the zone after "Uhr": "um 8:00 Uhr UTC".
  const uhr = `${h}:${pad2(minute)} Uhr${zone}`;

  if (dom === "*" && dow === "*") {
    return t(`Every day at ${time}`, `Täglich um ${uhr}`);
  }
  if (dom === "*") {
    const days = weekdays(dow);
    if (days === null) return custom;
    if (days.join() === "1,2,3,4,5") {
      return t(`Every weekday at ${time}`, `Werktags um ${uhr}`);
    }
    if (days.join() === "0,6") {
      return t(`Every weekend day at ${time}`, `Am Wochenende um ${uhr}`);
    }
    if (days.length === 7) {
      return t(`Every day at ${time}`, `Täglich um ${uhr}`);
    }
    // Monday first, as a week reads in both languages.
    const ordered = [...days.filter((d) => d !== 0), ...days.filter((d) => d === 0)];
    return t(
      `Every ${list(
        ordered.map((d) => DAYS_EN[d]),
        "and",
      )} at ${time}`,
      `Jeden ${list(
        ordered.map((d) => DAYS_DE[d]),
        "und",
      )} um ${uhr}`,
    );
  }
  const day = int(dom, 1, 31);
  if (day === null || dow !== "*") return custom;
  return t(`Every month on day ${day} at ${time}`, `Monatlich am ${day}. um ${uhr}`);
}
