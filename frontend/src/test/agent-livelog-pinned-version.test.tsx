import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi, beforeEach } from "vitest";
import type { ReactElement } from "react";

const { getMock } = vi.hoisted(() => ({ getMock: vi.fn() }));

vi.mock("@/lib/api", () => ({
  api: { get: (url: string) => getMock(url) },
}));

import { RunPinnedVersion } from "@/routes/agents.$id";

function renderIt(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("RunPinnedVersion", () => {
  beforeEach(() => {
    getMock.mockReset();
  });

  it("names the version a run is pinned to", () => {
    renderIt(<RunPinnedVersion versionNo={7} onOpenVersions={() => {}} />);
    expect(screen.getByText("v7")).toBeTruthy();
  });

  it("renders nothing for a run that predates version pinning", () => {
    const { container } = renderIt(<RunPinnedVersion versionNo={null} onOpenVersions={() => {}} />);
    // Null is the honest answer for a historical run -- migration 0099
    // deliberately did not backfill it -- and a badge reading "v?" would invite
    // somebody to go looking for a version that does not exist.
    expect(container.textContent).toBe("");
  });

  it("opens the Versions tab when clicked", async () => {
    const onOpenVersions = vi.fn();
    renderIt(<RunPinnedVersion versionNo={7} onOpenVersions={onOpenVersions} />);
    screen.getByRole("button", { name: /v7/ }).click();
    await waitFor(() => expect(onOpenVersions).toHaveBeenCalledTimes(1));
  });
});
