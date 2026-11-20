import { describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { renderHook } from "@testing-library/react";
import {
  usePauseCopilot,
  useResumeCopilot,
  useEndFollowup,
  useSetResponsibilityState,
  useDeleteCopilotNote,
  copilotKeys,
} from "@/lib/hooks-copilot";

vi.mock("@/lib/api", () => ({
  api: { post: vi.fn(), delete: vi.fn(), patch: vi.fn() },
}));

describe("usePauseCopilot", () => {
  it("calls api.post and invalidates profile, responsibilities, followups", async () => {
    const { api } = await import("@/lib/api");
    const mockPost = vi.mocked(api.post);
    mockPost.mockResolvedValue({ displayName: "Copilot", status: "paused" } as never);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");

    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );

    const { result } = renderHook(() => usePauseCopilot(), { wrapper });
    await result.current.mutateAsync();

    expect(mockPost).toHaveBeenCalledWith("/copilot/pause", {});
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: copilotKeys.profile });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: copilotKeys.responsibilities });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: copilotKeys.followups });
  });
});

describe("useResumeCopilot", () => {
  it("calls api.post and invalidates profile, responsibilities, followups", async () => {
    const { api } = await import("@/lib/api");
    const mockPost = vi.mocked(api.post);
    mockPost.mockResolvedValue({ displayName: "Copilot", status: "ready" } as never);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");

    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );

    const { result } = renderHook(() => useResumeCopilot(), { wrapper });
    await result.current.mutateAsync();

    expect(mockPost).toHaveBeenCalledWith("/copilot/resume", {});
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: copilotKeys.profile });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: copilotKeys.responsibilities });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: copilotKeys.followups });
  });
});

describe("useSetResponsibilityState", () => {
  it("calls api.patch and invalidates responsibilities, followups, profile", async () => {
    const { api } = await import("@/lib/api");
    const mockPatch = vi.mocked(api.patch);
    mockPatch.mockResolvedValue({ id: "r1", state: "done" } as never);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");

    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );

    const { result } = renderHook(() => useSetResponsibilityState(), { wrapper });
    await result.current.mutateAsync({ id: "r1", state: "done" });

    expect(mockPatch).toHaveBeenCalledWith("/copilot/responsibilities/r1", { state: "done" });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: copilotKeys.responsibilities });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: copilotKeys.followups });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: copilotKeys.profile });
  });
});

describe("useEndFollowup", () => {
  it("calls api.delete and invalidates followups", async () => {
    const { api } = await import("@/lib/api");
    const mockDelete = vi.mocked(api.delete);
    mockDelete.mockResolvedValue(undefined);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");

    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );

    const { result } = renderHook(() => useEndFollowup(), { wrapper });
    await result.current.mutateAsync("t1");

    expect(mockDelete).toHaveBeenCalledWith("/copilot/followups/t1");
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: copilotKeys.followups });
  });
});

describe("useDeleteCopilotNote", () => {
  it("calls api.delete and invalidates notes", async () => {
    const { api } = await import("@/lib/api");
    const mockDelete = vi.mocked(api.delete);
    mockDelete.mockResolvedValue(undefined);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");

    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );

    const { result } = renderHook(() => useDeleteCopilotNote(), { wrapper });
    await result.current.mutateAsync("n1");

    expect(mockDelete).toHaveBeenCalledWith("/copilot/notes/n1");
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: copilotKeys.notes });
  });
});
