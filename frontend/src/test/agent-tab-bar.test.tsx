// frontend/src/test/agent-tab-bar.test.tsx
// The Live Log is no tab of its own any more: the Live tab shows nearly the
// same. Its technical view stays reachable from the Live tab's "Technical
// view" link (?tab=livelog&runId=...), with a way back to Live.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { get: vi.fn(() => Promise.resolve([])), post: vi.fn() } };
});

import { AgentTabBar, LiveLog } from "@/routes/agents.$id";

describe("agent tab bar", () => {
  it("has no Live Log tab", () => {
    render(<AgentTabBar tab="overview" onSelect={() => {}} />);
    expect(screen.getByRole("button", { name: "Live" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Live Log/ })).toBeNull();
  });

  it("marks Live as current while the technical view is open", () => {
    render(<AgentTabBar tab="livelog" onSelect={() => {}} />);
    expect(screen.getByRole("button", { name: "Live" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("button", { name: "Overview" })).not.toHaveAttribute("aria-current");
  });

  it("selects a tab on click", () => {
    const onSelect = vi.fn();
    render(<AgentTabBar tab="overview" onSelect={onSelect} />);
    fireEvent.click(screen.getByRole("button", { name: "Live" }));
    expect(onSelect).toHaveBeenCalledWith("live");
  });
});

describe("technical view", () => {
  function renderLiveLog(onBack?: () => void) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <LiveLog agentId="a1" runId={null} onOpenVersions={() => {}} onBack={onBack} />
      </QueryClientProvider>,
    );
  }

  it("leads back to Live", () => {
    const onBack = vi.fn();
    renderLiveLog(onBack);
    fireEvent.click(screen.getByRole("button", { name: "Back to Live" }));
    expect(onBack).toHaveBeenCalled();
  });

  it("has no back button without a way back", () => {
    renderLiveLog();
    expect(screen.queryByRole("button", { name: "Back to Live" })).toBeNull();
  });
});
