// Task B2 (Foundation slice): the Cost Center "Export CSV" button used to
// fake a successful download (fixed toast.success, no Blob/fetch anywhere).
// `downloadUsageExport` is the real replacement -- same fetch-blob-anchor
// pattern as `audit-hooks.ts`'s `downloadAuditExport`, so this file mirrors
// `backup-panel.test.tsx`'s approach to that pattern: stub the `URL` statics
// jsdom doesn't implement, and assert on the fetch call plus the thrown
// error a failed response must produce (the whole point of the fix is that
// the caller's catch block can now actually fire).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { downloadUsageExport } from "@/lib/usage-export";

const TOKEN_KEY = "oc8-dev-token";

describe("downloadUsageExport", () => {
  beforeEach(() => {
    window.localStorage.setItem(TOKEN_KEY, "test-token");
    URL.createObjectURL = vi.fn(() => "blob:mock");
    URL.revokeObjectURL = vi.fn();
  });

  afterEach(() => {
    window.localStorage.removeItem(TOKEN_KEY);
    vi.unstubAllGlobals();
  });

  it("fetches /usage/export with the requested format, filters, and bearer token, then triggers a download", async () => {
    const blob = new Blob(["ts,agentId\n"]);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, status: 200, blob: async () => blob }),
    );
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

    await downloadUsageExport({ agentId: "agent-1", model: "claude-sonnet-4" }, "csv");

    const fetchMock = vi.mocked(fetch);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/usage/export?");
    expect(String(url)).toContain("agent_id=agent-1");
    expect(String(url)).toContain("model=claude-sonnet-4");
    expect(String(url)).toContain("format=csv");
    expect((init?.headers as Record<string, string>).authorization).toBe("Bearer test-token");
    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(URL.createObjectURL).toHaveBeenCalledWith(blob);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:mock");

    clickSpy.mockRestore();
  });

  it("throws (rather than silently downloading nothing) when the server responds with an error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 500 }));

    await expect(downloadUsageExport({}, "csv")).rejects.toThrow("export failed: 500");
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it("propagates a network failure the same way, for the caller's error toast", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));

    await expect(downloadUsageExport({}, "jsonl")).rejects.toThrow("network down");
  });
});
