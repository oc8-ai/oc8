// Signed out on an agent's page, signed back in on the dashboard: the place
// was lost along with the session. The gate now remembers where the person
// was, and the sign-in goes back there -- once, and only to a page of this app.
import { beforeEach, describe, expect, it } from "vitest";

import { rememberReturnPath, takeReturnPath } from "@/lib/return-path";

beforeEach(() => window.sessionStorage.clear());

describe("return path", () => {
  it("goes back to the page the session ended on, with its query, once", () => {
    rememberReturnPath("/agents/a1", "?tab=config");
    expect(takeReturnPath()).toBe("/agents/a1?tab=config");
    expect(takeReturnPath()).toBe("/");
  });

  it("goes home when nothing was remembered", () => {
    expect(takeReturnPath()).toBe("/");
  });

  it("never sends anyone off-site or back to the sign-in", () => {
    rememberReturnPath("//evil.example/x", "");
    expect(takeReturnPath()).toBe("/");
    rememberReturnPath("/login", "");
    expect(takeReturnPath()).toBe("/");
    window.sessionStorage.setItem("oc8-return-to", "https://evil.example/");
    expect(takeReturnPath()).toBe("/");
  });
});
