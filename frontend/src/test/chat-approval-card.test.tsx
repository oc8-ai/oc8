import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render as rtlRender, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Approval } from "@/lib/hooks";

const m = vi.hoisted(() => ({
  decide: vi.fn(),
  get: vi.fn(),
  lang: "en" as "en" | "de",
}));

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return { ...actual, useDecideApproval: () => ({ mutateAsync: m.decide, isPending: false }) };
});

// The data source, not the hook: the card reads the member's own row through
// the real `useApprovalStage` / `useMyGuardrails` over this `api.get`.
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, get: m.get } };
});

vi.mock("@/lib/i18n", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/i18n")>();
  return {
    ...actual,
    useT: () => (en: string, de?: string) => (m.lang === "de" ? (de ?? en) : en),
  };
});

import { ChatApprovalCard } from "@/components/chat-approval-card";
import { ApiError } from "@/lib/api";
import { stagePollInterval } from "@/lib/hooks-member-guardrails";

function render(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

/** `api.get` by path: my guardrails, and my own row read on its own. */
function serve(ownRow: Record<string, unknown> | "forbidden" | null) {
  m.get.mockImplementation(async (path: string) => {
    if (path.startsWith("/agents/")) {
      return {
        agentId: "a1",
        agentName: "Sales agent",
        summary: [],
        tools: [{ toolKey: "gmail", level: "ask_first", source: "member" }],
      };
    }
    if (path === "/approvals/ap1") {
      if (ownRow === "forbidden") throw new ApiError("forbidden", 403);
      if (ownRow === null) throw new ApiError("approval not found", 404);
      return ownRow;
    }
    throw new Error(`unexpected ${path}`);
  });
}

function approvalsCalls(): number {
  return m.get.mock.calls.filter(([path]) => String(path).startsWith("/approvals")).length;
}

const APPROVAL: Approval = {
  id: "ap1",
  agentId: "a1",
  agentName: "Sales agent",
  title: "Sales agent wants to call send_mail",
  detail: "",
  amount: null,
  time: "",
  status: "pending",
  actionType: "tool_send",
  toolName: "send_mail",
  toolArguments: { to: "kunde@x.de", subject: "Angebot" },
  reasonContext: { code: "always_requires_approval", connection: "gmail", right: "modify" },
  assigneeMemberId: "me",
  previousApprovalId: null,
  departmentName: "Sales",
};

beforeEach(() => {
  vi.clearAllMocks();
  m.lang = "en";
  serve(null);
});

describe("ChatApprovalCard", () => {
  it("shows the action, the member's own rule and labelled decision buttons", () => {
    render(<ChatApprovalCard approval={APPROVAL} onChangeRule={vi.fn()} />);
    expect(screen.getByText(/send mail to kunde@x\.de/)).toBeInTheDocument();
    expect(screen.getByText(/Angebot/)).toBeInTheDocument();
    expect(screen.getByText("Your rule: Gmail · Ask me first")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Approve: send mail to kunde@x.de" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Reject: send mail to kunde@x.de" }),
    ).toBeInTheDocument();
  });

  it("Change rule opens the sheet for that agent", () => {
    const onChangeRule = vi.fn();
    render(<ChatApprovalCard approval={APPROVAL} onChangeRule={onChangeRule} />);
    fireEvent.click(screen.getByRole("button", { name: "Change rule" }));
    expect(onChangeRule).toHaveBeenCalledWith("a1");
  });

  it("after approving with a department stage behind it, says who is next", async () => {
    m.decide.mockResolvedValue({ ...APPROVAL, status: "approved", nextStageStatus: "pending" });
    serve({ ...APPROVAL, status: "approved", nextStageStatus: "pending" });
    render(<ChatApprovalCard approval={APPROVAL} onChangeRule={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Approve: send mail to kunde@x.de" }));
    await waitFor(() =>
      expect(m.decide).toHaveBeenCalledWith({ approvalId: "ap1", decision: "approve" }),
    );
    expect(await screen.findByText("Approved by you · waiting for Sales")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Approve/ })).toBeNull();
  });

  it("says the same in German", async () => {
    m.lang = "de";
    m.decide.mockResolvedValue({ ...APPROVAL, status: "approved", nextStageStatus: "pending" });
    serve({ ...APPROVAL, status: "approved", nextStageStatus: "pending" });
    render(
      <ChatApprovalCard
        approval={{ ...APPROVAL, departmentName: "Vertrieb" }}
        onChangeRule={vi.fn()}
      />,
    );
    expect(await screen.findByText("Deine Regel: Gmail · Erst mich fragen")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^Freigeben: / }));
    expect(
      await screen.findByText("Von dir freigegeben · wartet auf Vertrieb"),
    ).toBeInTheDocument();
  });

  it("reads the department's rejection off my own row", async () => {
    m.decide.mockResolvedValue({ ...APPROVAL, status: "approved", nextStageStatus: "pending" });
    serve({ ...APPROVAL, status: "approved", nextStageStatus: "rejected" });
    render(<ChatApprovalCard approval={APPROVAL} onChangeRule={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Approve: send mail to kunde@x.de" }));
    expect(await screen.findByText("Rejected by Sales")).toBeInTheDocument();
    // Only my own row was read -- never a listing, never the department's queue.
    expect(m.get).toHaveBeenCalledWith("/approvals/ap1");
    expect(approvalsCalls()).toBe(
      m.get.mock.calls.filter(([path]) => path === "/approvals/ap1").length,
    );
  });

  it("an already-decided stage on a listed row needs no reading at all", async () => {
    render(
      <ChatApprovalCard
        approval={{ ...APPROVAL, status: "approved", nextStageStatus: "approved" }}
        onChangeRule={vi.fn()}
      />,
    );
    expect(await screen.findByText("Approved by Sales")).toBeInTheDocument();
    expect(approvalsCalls()).toBe(0);
  });

  it("a plain approval with no department stage reads nothing further", async () => {
    m.decide.mockResolvedValue({ ...APPROVAL, status: "approved", nextStageStatus: null });
    render(<ChatApprovalCard approval={APPROVAL} onChangeRule={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Approve: send mail to kunde@x.de" }));
    expect(await screen.findByText("Approved by you")).toBeInTheDocument();
    expect(approvalsCalls()).toBe(0);
  });

  it("a 403 on my own row is nothing to show, not an error", async () => {
    m.decide.mockResolvedValue({ ...APPROVAL, status: "approved", nextStageStatus: "pending" });
    serve("forbidden");
    render(<ChatApprovalCard approval={APPROVAL} onChangeRule={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Approve: send mail to kunde@x.de" }));
    expect(await screen.findByText("Approved by you · waiting for Sales")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("rejecting records it and says so", async () => {
    m.decide.mockResolvedValue({ ...APPROVAL, status: "rejected" });
    render(<ChatApprovalCard approval={APPROVAL} onChangeRule={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Reject: send mail to kunde@x.de" }));
    await waitFor(() =>
      expect(m.decide).toHaveBeenCalledWith({ approvalId: "ap1", decision: "reject" }),
    );
    expect(await screen.findByText("Rejected by you")).toBeInTheDocument();
  });

  it("a failed decision keeps the buttons and says why in one sentence", async () => {
    m.decide.mockRejectedValue(new Error("boom"));
    render(<ChatApprovalCard approval={APPROVAL} onChangeRule={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Approve: send mail to kunde@x.de" }));
    expect(await screen.findByText(/couldn't record your decision/i)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Approve: send mail to kunde@x.de" }),
    ).toBeInTheDocument();
  });
});

describe("polling the department's stage", () => {
  afterEach(() => vi.useRealTimers());

  it("re-reads while open and stops once the department decided", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    m.decide.mockResolvedValue({ ...APPROVAL, status: "approved", nextStageStatus: "pending" });
    serve({ ...APPROVAL, status: "approved", nextStageStatus: "pending" });
    render(<ChatApprovalCard approval={APPROVAL} onChangeRule={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Approve: send mail to kunde@x.de" }));
    await screen.findByText("Approved by you · waiting for Sales");
    await waitFor(() => expect(approvalsCalls()).toBe(1));

    serve({ ...APPROVAL, status: "approved", nextStageStatus: "rejected" });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(await screen.findByText("Rejected by Sales")).toBeInTheDocument();
    const settled = approvalsCalls();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30000);
    });
    expect(approvalsCalls()).toBe(settled);
  });

  it("stops when my own row is gone instead of polling forever", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    m.decide.mockResolvedValue({ ...APPROVAL, status: "approved", nextStageStatus: "pending" });
    serve(null);
    render(<ChatApprovalCard approval={APPROVAL} onChangeRule={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Approve: send mail to kunde@x.de" }));
    await screen.findByText("Approved by you · waiting for Sales");
    await waitFor(() => expect(approvalsCalls()).toBe(1));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30000);
    });
    expect(approvalsCalls()).toBe(1);
  });
});

describe("stagePollInterval", () => {
  it("polls only while the department's stage is open", () => {
    expect(stagePollInterval("pending")).toBe(5000);
    expect(stagePollInterval("approved")).toBe(false);
    expect(stagePollInterval("rejected")).toBe(false);
    expect(stagePollInterval("expired")).toBe(false);
    expect(stagePollInterval(null)).toBe(false);
  });
});
