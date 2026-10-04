import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { renderHook } from "@testing-library/react";
import { usePauseCopilot, useEndFollowup } from "@/lib/hooks-copilot";

vi.mock("@/lib/api", () => ({ api: { post: vi.fn(), delete: vi.fn() } }));

afterEach(() => {
  vi.restoreAllMocks();
});

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("usePauseCopilot", () => {
  it("calls api.post and invalidates copilotKeys.profile", async () => {
    const { api } = await import("@/lib/api");
    const mockPost = vi.mocked(api.post);
    mockPost.mockResolvedValue({ displayName: "Copilot", status: "paused" } as never);

    const { result } = renderHook(() => usePauseCopilot(), { wrapper });

    await result.current.mutateAsync();

    expect(mockPost).toHaveBeenCalledWith("/copilot/pause", {});
  });
});

describe("useEndFollowup", () => {
  it("calls api.delete with followup id", async () => {
    const { api } = await import("@/lib/api");
    const mockDelete = vi.mocked(api.delete);
    mockDelete.mockResolvedValue(undefined);

    const { result } = renderHook(() => useEndFollowup(), { wrapper });

    await result.current.mutateAsync("t1");

    expect(mockDelete).toHaveBeenCalledWith("/copilot/followups/t1");
  });
});
