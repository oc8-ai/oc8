import { describe, expect, it } from "vitest";

import { browserTimezone, scheduleZone, timezoneOptions } from "./timezones";

describe("timezones", () => {
  it("reads a schedule without a zone as UTC", () => {
    expect(scheduleZone(null)).toBe("UTC");
    expect(scheduleZone(undefined)).toBe("UTC");
    expect(scheduleZone("")).toBe("UTC");
    expect(scheduleZone("Europe/Berlin")).toBe("Europe/Berlin");
  });

  it("offers UTC first, once, and the IANA zones after it", () => {
    const zones = timezoneOptions();
    expect(zones[0]).toBe("UTC");
    expect(zones.filter((z) => z === "UTC")).toHaveLength(1);
    expect(zones).toContain("Europe/Berlin");
  });

  it("knows this browser's zone", () => {
    expect(browserTimezone()).toBeTruthy();
  });
});
