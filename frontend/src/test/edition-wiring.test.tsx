import { readFileSync } from "node:fs";
import { join } from "node:path";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

const { post } = vi.hoisted(() => ({ post: vi.fn() }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post, put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

import { useCompleteMemberOnboarding, useSkipMemberOnboarding } from "@/lib/hooks";

const src = (path: string) => readFileSync(join(process.cwd(), path), "utf-8");

describe("edition wiring", () => {
  it("renders the edition sidebar header above the navigation and the footer below it", () => {
    const shell = src("src/components/app-shell.tsx");
    const header = shell.indexOf("<EditionSidebarHeader collapsed={collapsed} />");
    const nav = shell.indexOf('<nav className="flex-1');
    const navEnd = shell.indexOf("</nav>");
    const footer = shell.indexOf("<EditionSidebarFooter collapsed={collapsed} />");
    expect(header).toBeGreaterThan(-1);
    expect(header).toBeLessThan(nav);
    expect(footer).toBeGreaterThan(navEnd);
  });

  it("renders edition onboarding routes without the app shell", () => {
    const root = src("src/routes/__root.tsx");
    expect(root).toContain("editionBarePaths(composeFrontendEdition(editionExtensions))");
    expect(root).toContain("EDITION_BARE_ROUTES.has(pathname)");
  });

  it("lets the office page ask onboardingRedirect", () => {
    const index = src("src/routes/index.tsx");
    expect(index).toContain("onboardingRedirect(");
    expect(index).not.toContain("wantsOnboarding");
  });

  it("appends edition profile sections to the profile page", () => {
    const profile = src("src/routes/profile.tsx");
    expect(profile.indexOf("<EditionProfileSections />")).toBeGreaterThan(
      profile.indexOf("<ApprovalChannelsPanel />"),
    );
  });

  it("gives the chat composer the edition focus glow hook", () => {
    expect(src("src/components/chat-window.tsx")).toContain("edition-accent-focus");
    expect(src("src/styles.css")).toContain("var(--edition-accent-glow, transparent)");
  });
});

describe("member onboarding hooks", () => {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>
  );

  it("posts complete and skip for the caller", async () => {
    post.mockResolvedValue({ memberOnboardingStatus: "completed" });
    const complete = renderHook(() => useCompleteMemberOnboarding(), { wrapper });
    await complete.result.current.mutateAsync();
    const skip = renderHook(() => useSkipMemberOnboarding(), { wrapper });
    await skip.result.current.mutateAsync();
    await waitFor(() =>
      expect(post.mock.calls.map((c) => c[0])).toEqual([
        "/onboarding/member/complete",
        "/onboarding/member/skip",
      ]),
    );
  });
});
