// frontend/src/test/run-unreadable.test.tsx
// A run the reader may not read (a personal agent's, behind an admin's
// blast_radius card or a `?runId=` link) answers 404. That refusal is final:
// `useRun` does not ask again.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const get = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async (orig) => {
  const actual = await orig<typeof import("@/lib/api")>();
  return { ...actual, api: { get, post: vi.fn() } };
});

import { ApiError } from "@/lib/api";
import { useRun } from "@/lib/hooks";

describe("useRun", () => {
  it("does not retry a 404", async () => {
    get.mockImplementation(() => Promise.reject(new ApiError("run not found", 404)));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: 3, retryDelay: 0 } } });
    const { result } = renderHook(() => useRun("r1"), {
      wrapper: ({ children }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>,
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(get).toHaveBeenCalledTimes(1);
  });
});
