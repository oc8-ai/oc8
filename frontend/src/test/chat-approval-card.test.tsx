import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Approval } from "@/lib/hooks";

const m = vi.hoisted(() => ({
  decide: vi.fn(),
  followUp: null as Record<string, unknown> | null,
  followUpArg: [] as unknown[],
  lang: "en" as "en" | "de",
}));

vi.mock("@/lib/hooks", () => ({
  useDecideApproval: () => ({ mutateAsync: m.decide, isPending: false }),
}));

vi.mock("@/lib/hooks-member-guardrails", () => ({
  useApprovalFollowUp: (id: string | null) => {
    m.followUpArg.push(id);
    return { data: id ? m.followUp : null };
  },
  useMyGuardrails: () => ({
    data: {
      agentId: "a1",
      agentName: "Sales agent",
      summary: [],
      tools: [{ toolKey: "gmail", level: "ask_first", source: "member" }],
    },
  }),
}));

vi.mock("@/lib/i18n", () => ({
  useT: () => (en: string, de?: string) => (m.lang === "de" ? (de ?? en) : en),
}));

import { ChatApprovalCard } from "@/components/chat-approval-card";

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
  m.followUp = null;
  m.followUpArg = [];
  m.lang = "en";
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
    m.decide.mockResolvedValue({ ...APPROVAL, status: "approved", resumed: false });
    render(<ChatApprovalCard approval={APPROVAL} onChangeRule={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Approve: send mail to kunde@x.de" }));
    await waitFor(() =>
      expect(m.decide).toHaveBeenCalledWith({ approvalId: "ap1", decision: "approve" }),
    );
    expect(await screen.findByText("Approved by you · waiting for Sales")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Approve/ })).toBeNull();
    // The department stage is followed by the member's row id.
    expect(m.followUpArg).toContain("ap1");
  });

  it("says the same in German", async () => {
    m.lang = "de";
    m.decide.mockResolvedValue({ ...APPROVAL, status: "approved", resumed: false });
    render(
      <ChatApprovalCard
        approval={{ ...APPROVAL, departmentName: "Vertrieb" }}
        onChangeRule={vi.fn()}
      />,
    );
    expect(screen.getByText("Deine Regel: Gmail · Erst mich fragen")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^Freigeben: / }));
    expect(
      await screen.findByText("Von dir freigegeben · wartet auf Vertrieb"),
    ).toBeInTheDocument();
  });

  it("shows a department rejection of the second stage", async () => {
    m.decide.mockResolvedValue({ ...APPROVAL, status: "approved", resumed: false });
    m.followUp = { ...APPROVAL, id: "ap2", previousApprovalId: "ap1", status: "rejected" };
    render(<ChatApprovalCard approval={APPROVAL} onChangeRule={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Approve: send mail to kunde@x.de" }));
    expect(await screen.findByText("Rejected by Sales")).toBeInTheDocument();
  });

  it("a plain approval with nobody behind it just says approved", async () => {
    m.decide.mockResolvedValue({ ...APPROVAL, status: "approved", resumed: true });
    render(<ChatApprovalCard approval={APPROVAL} onChangeRule={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Approve: send mail to kunde@x.de" }));
    expect(await screen.findByText("Approved by you")).toBeInTheDocument();
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
