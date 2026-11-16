import { afterEach, describe, expect, it, vi } from "vitest";

import type { EditionServerHooks } from "../edition/contracts";
import { isAllowedAncestor, withFrameAncestors } from "./frame-ancestors";

const request = new Request("https://app.example.com/");
const csp = (res: Response) => res.headers.get("content-security-policy");
const hooksFor = (origins: readonly string[]): EditionServerHooks => ({
  frameAncestors: async () => origins,
});

afterEach(() => {
  vi.useRealTimers();
});

describe("withFrameAncestors", () => {
  it("sets frame-ancestors 'self' without hooks", async () => {
    const res = await withFrameAncestors(request, new Response("ok"), {});
    expect(csp(res)).toBe("frame-ancestors 'self'");
    expect(await res.text()).toBe("ok");
  });

  it("appends hook origins", async () => {
    const res = await withFrameAncestors(
      request,
      new Response("ok"),
      hooksFor(["https://erp.example.com"]),
    );
    expect(csp(res)).toBe("frame-ancestors 'self' https://erp.example.com");
  });

  it("drops invalid origins", async () => {
    const res = await withFrameAncestors(
      request,
      new Response("ok"),
      hooksFor([
        "https://ok.example.com",
        "https://x.com; script-src *",
        "*",
        "http://evil.com",
        "https://a.com/path",
        "http://localhost:8069",
      ]),
    );
    expect(csp(res)).toBe("frame-ancestors 'self' https://ok.example.com http://localhost:8069");
  });

  it("falls back to 'self' when the hook throws", async () => {
    const res = await withFrameAncestors(request, new Response("ok"), {
      frameAncestors: async () => {
        throw new Error("boom");
      },
    });
    expect(csp(res)).toBe("frame-ancestors 'self'");
  });

  it("falls back to 'self' when the hook is slower than 2000 ms", async () => {
    vi.useFakeTimers();
    const pending = withFrameAncestors(request, new Response("ok"), {
      frameAncestors: () =>
        new Promise((resolve) => setTimeout(() => resolve(["https://late.example.com"]), 5000)),
    });
    await vi.advanceTimersByTimeAsync(2001);
    expect(csp(await pending)).toBe("frame-ancestors 'self'");
  });

  it("keeps an existing CSP and appends the directive", async () => {
    const res = await withFrameAncestors(
      request,
      new Response("ok", { headers: { "content-security-policy": "script-src 'self'" } }),
      {},
    );
    expect(csp(res)).toBe("script-src 'self'; frame-ancestors 'self'");
  });

  it("replaces an existing frame-ancestors directive", async () => {
    const res = await withFrameAncestors(
      request,
      new Response("ok", {
        headers: { "content-security-policy": "script-src 'self'; frame-ancestors *" },
      }),
      hooksFor(["https://erp.example.com"]),
    );
    expect(csp(res)).toBe("script-src 'self'; frame-ancestors 'self' https://erp.example.com");
  });

  it("keeps status and other headers", async () => {
    const res = await withFrameAncestors(
      request,
      new Response("nope", { status: 404, headers: { "x-test": "1" } }),
      {},
    );
    expect(res.status).toBe(404);
    expect(res.headers.get("x-test")).toBe("1");
  });
});

describe("isAllowedAncestor", () => {
  it("accepts https hosts and local http", () => {
    expect(isAllowedAncestor("https://a.example.com:8443")).toBe(true);
    expect(isAllowedAncestor("http://127.0.0.1:8069")).toBe(true);
    expect(isAllowedAncestor("http://example.com")).toBe(false);
  });
});
