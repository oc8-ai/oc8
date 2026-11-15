import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { AgentBuilderDTO, BuilderBoardDTO } from "@/lib/agent-builder-api";
import { useApplyAgentBuilder, useRequestAgentBuilder } from "@/lib/agent-builder-api";
import { useBoards } from "@/lib/boards-api";

vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn() },
  retryUnlessRefused: () => false,
}));

function dto(status: AgentBuilderDTO["status"], board: BuilderBoardDTO | null): AgentBuilderDTO {
  return { id: "p1", status, kind: "board", board } as AgentBuilderDTO;
}

const changed: BuilderBoardDTO = {
  boardId: "b1",
  mode: "update",
  title: "Sales",
  datasets: [],
  blockCount: 2,
};

function setup() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
  vi.mocked(api.get).mockResolvedValue([]);
  renderHook(() => useBoards(), { wrapper });
  qc.setQueryData(["boards", "b1"], { id: "b1" });
  qc.setQueryData(["boards", "b1", "query", { dataset: "sales" }], { series: [] });
  qc.setQueryData(["boards", "b2"], { id: "b2" });
  return { qc, wrapper };
}

afterEach(() => {
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
});

describe("useApplyAgentBuilder", () => {
  it("refreshes the dashboard it applied and the board lists", async () => {
    const { qc, wrapper } = setup();
    await waitFor(() => expect(qc.getQueryState(["boards", "all"])?.status).toBe("success"));
    vi.mocked(api.post).mockResolvedValue(dto("applied", changed));
    const { result } = renderHook(() => useApplyAgentBuilder("p1"), { wrapper });
    result.current.mutate();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(qc.getQueryState(["boards", "b1"])?.isInvalidated).toBe(true);
    expect(qc.getQueryState(["boards", "b1", "query", { dataset: "sales" }])?.isInvalidated).toBe(
      true,
    );
    // The mounted list reads again; another board is left alone.
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
    expect(qc.getQueryState(["boards", "b2"])?.isInvalidated).toBe(false);
  });

  it("leaves the boards alone when nothing about a dashboard was applied", async () => {
    const { qc, wrapper } = setup();
    vi.mocked(api.post).mockResolvedValue(dto("applied", null));
    const apply = renderHook(() => useApplyAgentBuilder("p1"), { wrapper });
    apply.result.current.mutate();
    await waitFor(() => expect(apply.result.current.isSuccess).toBe(true));
    vi.mocked(api.post).mockResolvedValue(dto("awaiting_approval", changed));
    const request = renderHook(() => useRequestAgentBuilder("p1"), { wrapper });
    request.result.current.mutate();
    await waitFor(() => expect(request.result.current.isSuccess).toBe(true));
    expect(qc.getQueryState(["boards", "b1"])?.isInvalidated).toBe(false);
  });
});
