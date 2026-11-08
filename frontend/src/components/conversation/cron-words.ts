// A schedule's cron expression in words, for the Live tab's trigger line.

type Translate = (en: string, de?: string) => string;

/** A schedule in words where it is simple ("daily 09:00", "weekdays
 *  07:30"); any other cron expression stays as it is. */
export function cronWords(cron: string, t: Translate): string {
  const m = /^(\d{1,2}) (\d{1,2}) \* \* (\*|1-5)$/.exec(cron.trim());
  if (!m) return cron;
  const time = `${m[2].padStart(2, "0")}:${m[1].padStart(2, "0")}`;
  return (
    m[3] === "*" ? t("daily {time}", "täglich {time}") : t("weekdays {time}", "werktags {time}")
  ).replace("{time}", time);
}
