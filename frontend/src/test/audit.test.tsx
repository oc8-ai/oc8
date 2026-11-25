import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AuditEvent, AuditFilters } from "@/lib/audit-hooks";

const h = vi.hoisted(() => ({
  navigate: vi.fn(),
  filters: [] as AuditFilters[],
  events: [] as AuditEvent[],
}));

vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return { ...actual, useNavigate: () => h.navigate };
});
vi.mock("@/lib/audit-hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/audit-hooks")>();
  return {
    ...actual,
    useAuditEvents: (filters: AuditFilters) => {
      h.filters.push(filters);
      return {
        data: { pages: [{ events: h.events, nextBeforeSeq: null }] },
        fetchNextPage: vi.fn(),
        hasNextPage: false,
        isFetchingNextPage: false,
        isError: false,
        error: null,
      };
    },
    useAuditIntegrity: () => ({ data: undefined, isError: false, error: null }),
    useVerifyChain: () => ({ mutate: vi.fn(), isPending: false }),
  };
});

import { downloadAuditExport } from "@/lib/audit-hooks";
import { AuditPage, Route } from "@/routes/audit";

const RUN = "0192f7a4-5b6c-7d8e-9f01-23456789abcd";
const CHAT = "0192f7a4-5b6c-7d8e-9f01-23456789abce";

function toolResult(): AuditEvent {
  return {
    id: "e1",
    seq: 7,
    ts: "2026-11-25T10:00:00Z",
    actorType: "agent",
    actorId: null,
    category: "tool_action",
    action: "tool.result:search_records",
    resource: {
      tool: "search_records",
      run_id: RUN,
      connection: "odoo",
      outcome: "error",
      duration_ms: 1234,
    },
    decision: "allow",
    reason: null,
    responsibleType: "agent",
    responsibleId: "a1",
    hash: "ab",
    prevHash: "cd",
  };
}

describe("/audit", () => {
  beforeEach(() => {
    h.navigate.mockReset();
    h.filters = [];
    h.events = [];
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("the audit page filters by run from the URL", () => {
    vi.spyOn(Route, "useSearch").mockReturnValue({ run_id: RUN } as never);
    render(<AuditPage />);
    expect(screen.getByPlaceholderText("Run")).toHaveValue(RUN);
    expect(h.filters.at(-1)?.runId).toBe(RUN);
    expect(h.filters.at(-1)?.chatSessionId).toBeUndefined();
    expect(h.navigate).not.toHaveBeenCalled();
  });

  it("reads only well-formed ids from the URL", () => {
    const validate = Route.options.validateSearch as (s: Record<string, unknown>) => unknown;
    expect(validate({ run_id: RUN, chat_session_id: CHAT, other: "x" })).toEqual({
      run_id: RUN,
      chat_session_id: CHAT,
    });
    expect(validate({ run_id: "../../members", chat_session_id: 4 })).toEqual({});
  });

  it("writes a typed chat id into the URL and the filter", () => {
    vi.useFakeTimers();
    vi.spyOn(Route, "useSearch").mockReturnValue({ run_id: RUN } as never);
    render(<AuditPage />);
    fireEvent.change(screen.getByPlaceholderText("Chat"), { target: { value: CHAT } });
    act(() => {
      vi.advanceTimersByTime(400);
    });
    expect(h.filters.at(-1)).toMatchObject({ runId: RUN, chatSessionId: CHAT });
    expect(h.navigate).toHaveBeenLastCalledWith({
      to: "/audit",
      search: { run_id: RUN, chat_session_id: CHAT },
      replace: true,
    });
  });

  it("does not send a half-typed id to the server", () => {
    vi.useFakeTimers();
    vi.spyOn(Route, "useSearch").mockReturnValue({} as never);
    render(<AuditPage />);
    fireEvent.change(screen.getByPlaceholderText("Run"), { target: { value: "0192f7a4" } });
    act(() => {
      vi.advanceTimersByTime(400);
    });
    expect(screen.getByPlaceholderText("Run")).toHaveValue("0192f7a4");
    expect(h.filters.at(-1)?.runId).toBeUndefined();
    expect(h.navigate).not.toHaveBeenCalled();
  });

  it("shows a tool result's outcome and duration in its detail", () => {
    vi.spyOn(Route, "useSearch").mockReturnValue({} as never);
    h.events = [toolResult()];
    render(<AuditPage />);
    fireEvent.click(screen.getByText("tool.result:search_records"));
    expect(screen.getByText("Outcome")).toBeInTheDocument();
    expect(screen.getByText("error")).toBeInTheDocument();
    expect(screen.getByText("1234 ms")).toBeInTheDocument();
  });

  it("exports with the run and chat filters", async () => {
    window.localStorage.setItem("oc8-dev-token", "test-token");
    URL.createObjectURL = vi.fn(() => "blob:mock");
    URL.revokeObjectURL = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, status: 200, blob: async () => new Blob([""]) }),
    );
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    await downloadAuditExport({ runId: RUN, chatSessionId: CHAT }, "jsonl");
    const url = String(vi.mocked(fetch).mock.calls[0][0]);
    expect(url).toContain(`run_id=${RUN}`);
    expect(url).toContain(`chat_session_id=${CHAT}`);
    vi.unstubAllGlobals();
    window.localStorage.removeItem("oc8-dev-token");
  });
});
