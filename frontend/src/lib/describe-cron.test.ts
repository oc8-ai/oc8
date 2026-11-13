import { describe, expect, it } from "vitest";

import { describeCron } from "./describe-cron";
import type { Translate } from "./tool-labels";

const en: Translate = (english) => english;
const de: Translate = (english, german) => german ?? english;

describe("describeCron", () => {
  it.each([
    ["0 8 * * *", "Every day at 8:00", "Täglich um 8:00 Uhr"],
    ["30 17 * * *", "Every day at 17:30", "Täglich um 17:30 Uhr"],
    ["0 9 * * 1-5", "Every weekday at 9:00", "Werktags um 9:00 Uhr"],
    ["0 9 * * 1,2,3,4,5", "Every weekday at 9:00", "Werktags um 9:00 Uhr"],
    ["0 10 * * 6,0", "Every weekend day at 10:00", "Am Wochenende um 10:00 Uhr"],
    ["15 7 * * 1", "Every Monday at 7:15", "Jeden Montag um 7:15 Uhr"],
    [
      "0 8 * * 0,1,4",
      "Every Monday, Thursday and Sunday at 8:00",
      "Jeden Montag, Donnerstag und Sonntag um 8:00 Uhr",
    ],
    ["0 6 1 * *", "Every month on day 1 at 6:00", "Monatlich am 1. um 6:00 Uhr"],
    ["0 * * * *", "Every hour", "Jede Stunde"],
    ["5 * * * *", "Every hour at :05", "Jede Stunde um :05"],
    ["0 */4 * * *", "Every 4 hours", "Alle 4 Stunden"],
    ["*/15 * * * *", "Every 15 minutes", "Alle 15 Minuten"],
    ["*/1 * * * *", "Every minute", "Jede Minute"],
  ])("%s", (expr, english, german) => {
    expect(describeCron(expr, en)).toBe(english);
    expect(describeCron(expr, de)).toBe(german);
  });

  it("names the time zone after a time of day", () => {
    expect(describeCron("0 8 * * *", en, "UTC")).toBe("Every day at 8:00 UTC");
    expect(describeCron("0 8 * * 1", de, "UTC")).toBe("Jeden Montag um 8:00 Uhr UTC");
    expect(describeCron("*/15 * * * *", en, "UTC")).toBe("Every 15 minutes");
  });

  it.each(["0 8 1 1 *", "0 8 1-7 * 1", "0 8,12 * * *", "@daily", "0 8 * *", "0 25 * * *"])(
    "keeps %s as a custom schedule",
    (expr) => {
      expect(describeCron(expr, en)).toBe(`Custom schedule (${expr})`);
      expect(describeCron(expr, de)).toBe(`Benutzerdefiniert (${expr})`);
    },
  );
});
