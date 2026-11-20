import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api";
import type { Approval } from "@/lib/hooks";

const m = vi.hoisted(() => ({
  grants: [] as unknown[],
  decide: vi.fn(),
  revoke: vi.fn(),
}));
vi.mock("@/lib/direct-access-api", () => ({
  useDirectAccessGrants: () => ({ data: m.grants }),
  useDecideDirectAccess: () => ({ mutate: m.decide, isPending: false }),
  useRevokeDirectAccess: () => ({ mutate: m.revoke, isPending: false }),
  useMyDirectAccessApprovals: () => ({ data: [] }),
}));

import { DirectAccessBanner, DirectAccessCard } from "@/components/conversation/direct-access-card";
import { TURN_APPROVAL_CARDS } from "@/components/conversation/turn-approval-registry";
import { LanguageProvider } from "@/lib/i18n";

const offer: Approval = {
  id: "ap1",
  agentId: "copilot",
  title: "Copilot on behalf of Anna: read Odoo",
  detail: "Find the quotation for XYZ GmbH",
  amount: null,
  time: "",
  status: "pending",
  actionType: "direct_access",
  runId: "r1",
  directAccess: {
    sessionId: "s1",
    toolKey: "odoo",
    label: "Odoo",
    memberName: "Anna",
    account: "organisation",
    decider: "member",
    grantId: null,
    grantActive: false,
  },
};

const write: Approval = {
  ...offer,
  id: "ap2",
  actionType: "tool_send",
  title: "oc8 Assistant on behalf of Anna wants to call update_record",
  recordUrl: "https://erp.example.com/odoo/sale.order/42",
  directAccess: { ...offer.directAccess!, account: "own" },
};

const grant = {
  id: "g1",
  sessionId: "s1",
  toolKey: "odoo",
  label: "Odoo",
  approvalId: "ap1",
  grantedAt: "",
};

describe("direct access in the chat", () => {
  beforeEach(() => {
    m.grants = [];
    m.decide.mockReset();
    m.revoke.mockReset();
  });

  it("is registered as a turn approval card", () => {
    expect(TURN_APPROVAL_CARDS.direct_access).toBeDefined();
  });

  it("asks the person to allow access, naming connection and account", () => {
    render(<DirectAccessCard approval={offer} />);
    expect(screen.getByText(/Odoo/)).toBeInTheDocument();
    expect(screen.getByText(/on behalf of Anna/i)).toBeInTheDocument();
    expect(screen.getByText(/organisation account/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /allow/i }));
    expect(m.decide).toHaveBeenCalledWith(
      expect.objectContaining({ approvalId: "ap1", decision: "approve" }),
      expect.anything(),
    );
  });

  it("shows the record link on a write and lets the person reject it", () => {
    render(<DirectAccessCard approval={write} />);
    const link = screen.getByRole("link", { name: /open the record/i });
    expect(link).toHaveAttribute("href", "https://erp.example.com/odoo/sale.order/42");
    expect(screen.getByText(/your own login/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /reject/i }));
    expect(m.decide).toHaveBeenCalledWith(
      expect.objectContaining({ approvalId: "ap2", decision: "reject" }),
      expect.anything(),
    );
  });

  it("offers revoke while the grant is active", () => {
    m.grants = [grant];
    render(<DirectAccessCard approval={{ ...offer, status: "approved" }} />);
    fireEvent.click(screen.getByRole("button", { name: /revoke access/i }));
    expect(m.revoke).toHaveBeenCalledWith("g1");
  });

  it("leaves a department-held write to the department", () => {
    render(
      <DirectAccessCard
        approval={{ ...write, directAccess: { ...write.directAccess!, decider: "department" } }}
      />,
    );
    expect(screen.queryByRole("button", { name: /approve/i })).toBeNull();
    expect(screen.getByText(/department/i)).toBeInTheDocument();
  });

  it("shows the person's approved write as waiting for the department", () => {
    render(
      <DirectAccessCard
        approval={{
          ...write,
          status: "approved",
          directAccess: { ...write.directAccess!, awaitingDepartment: true },
        }}
      />,
    );
    expect(screen.getByText(/waiting for the department's approval/i)).toBeInTheDocument();
    expect(screen.queryByText(/^approved$/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /approve/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /reject/i })).toBeNull();
  });

  it("switches to waiting for the department once the person approves", () => {
    m.decide.mockImplementation((_vars: unknown, opts: { onSuccess: (dto: Approval) => void }) =>
      opts.onSuccess({
        ...write,
        status: "approved",
        directAccess: { ...write.directAccess!, awaitingDepartment: true },
      }),
    );
    render(<DirectAccessCard approval={write} />);
    fireEvent.click(screen.getByRole("button", { name: /approve/i }));
    expect(screen.getByText(/waiting for the department's approval/i)).toBeInTheDocument();
  });

  it("says nothing about the account when it could not be resolved", () => {
    render(
      <DirectAccessCard
        approval={{ ...offer, directAccess: { ...offer.directAccess!, account: null } }}
      />,
    );
    expect(screen.queryByText(/organisation account/i)).toBeNull();
    expect(screen.queryByText(/your own login/i)).toBeNull();
    expect(screen.getByText(/account could not be determined/i)).toBeInTheDocument();
  });

  it("renders the German title for a German reader, the English one otherwise", () => {
    const localized: Approval = {
      ...offer,
      titleTranslations: { de: "Copilot im Auftrag von Anna: Odoo lesen" },
    };
    const { unmount } = render(<DirectAccessCard approval={localized} />);
    expect(screen.getByText(/Copilot on behalf of Anna: read Odoo/)).toBeInTheDocument();
    unmount();
    window.localStorage.setItem("oc8-lang", "de");
    try {
      render(
        <LanguageProvider>
          <DirectAccessCard approval={localized} />
        </LanguageProvider>,
      );
      expect(screen.getByText(/Copilot im Auftrag von Anna: Odoo lesen/)).toBeInTheDocument();
    } finally {
      window.localStorage.removeItem("oc8-lang");
    }
  });

  it("shows a banner per active grant with revoke", () => {
    m.grants = [grant];
    render(<DirectAccessBanner sessionId="s1" />);
    expect(screen.getByRole("status")).toHaveTextContent(/Direct access: Odoo \(read\)/);
    fireEvent.click(screen.getByRole("button", { name: /revoke/i }));
    expect(m.revoke).toHaveBeenCalledWith("g1");
  });

  it("shows no banner without a grant", () => {
    const { container } = render(<DirectAccessBanner sessionId="s1" />);
    expect(container).toBeEmptyDOMElement();
  });
  describe("with a fitting agent", () => {
    const withAgents: Approval = {
      ...offer,
      directAccess: {
        ...offer.directAccess!,
        loginName: "Shared login",
        request: "Find the quotation for XYZ GmbH",
        agents: [
          { id: "a1", name: "Sales Agent", department: "Sales", score: 0.9 },
          { id: "a2", name: "Quote Agent", department: "Sales", score: 0.7 },
        ],
      },
    };

    it("shows the fitting agent and runs it", () => {
      render(<DirectAccessCard approval={withAgents} />);
      expect(screen.getByText("Sales Agent does this — run it?")).toBeInTheDocument();
      expect(screen.getByText(/Hands over:/)).toBeInTheDocument();
      expect(screen.getByText(/Find the quotation for XYZ GmbH/)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /^allow$/i })).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "Run agent" }));
      expect(m.decide).toHaveBeenCalledWith(
        { approvalId: "ap1", decision: "approve", agentId: "a1" },
        expect.anything(),
      );
    });

    it("does it itself with the named login", () => {
      render(<DirectAccessCard approval={withAgents} />);
      fireEvent.click(screen.getByRole("button", { name: "Do it myself with Shared login" }));
      const [vars] = m.decide.mock.calls[0];
      expect(vars).toEqual({ approvalId: "ap1", decision: "approve" });
      expect(vars).not.toHaveProperty("agentId");
    });

    it("offers the other fitting agents", () => {
      render(<DirectAccessCard approval={withAgents} />);
      expect(screen.getByText(/Also fits:/)).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Run Quote Agent" }));
      expect(m.decide).toHaveBeenCalledWith(
        { approvalId: "ap1", decision: "approve", agentId: "a2" },
        expect.anything(),
      );
    });

    it("shows that the chosen agent is on it after reload", () => {
      render(
        <DirectAccessCard
          approval={{
            ...withAgents,
            status: "approved",
            directAccess: {
              ...withAgents.directAccess!,
              chosenAgent: { id: "a1", name: "Sales Agent", runId: "run9" },
            },
          }}
        />,
      );
      const link = screen.getByRole("link", { name: /Sales Agent is on it\./ });
      expect(link).toHaveAttribute("href", "/activity?run=run9");
      expect(screen.queryByRole("button")).toBeNull();
    });

    it("explains when the agent can no longer be run", () => {
      m.decide.mockImplementation((_vars: unknown, opts: { onError: (e: Error) => void }) =>
        opts.onError(
          new ApiError('{"code":"agent_not_runnable"}', 403, {
            detail: { code: "agent_not_runnable" },
          }),
        ),
      );
      render(<DirectAccessCard approval={withAgents} />);
      fireEvent.click(screen.getByRole("button", { name: "Run agent" }));
      expect(screen.getByText("You can no longer run Sales Agent.")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Run agent" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /reject/i })).toBeInTheDocument();
    });

    it("names the agent each other button runs, in German too", () => {
      window.localStorage.setItem("oc8-lang", "de");
      try {
        render(
          <LanguageProvider>
            <DirectAccessCard approval={withAgents} />
          </LanguageProvider>,
        );
        expect(screen.getByRole("button", { name: "Quote Agent starten" })).toBeInTheDocument();
      } finally {
        window.localStorage.removeItem("oc8-lang");
      }
    });

    it("clears the refusal once a later answer succeeds", () => {
      m.decide.mockImplementationOnce((_vars: unknown, opts: { onError: (e: Error) => void }) =>
        opts.onError(
          new ApiError('{"code":"agent_not_runnable"}', 403, {
            detail: { code: "agent_not_runnable" },
          }),
        ),
      );
      m.decide.mockImplementationOnce(
        (_vars: unknown, opts: { onSuccess: (dto: unknown) => void }) =>
          opts.onSuccess({ directAccess: {} }),
      );
      render(<DirectAccessCard approval={withAgents} />);
      fireEvent.click(screen.getByRole("button", { name: "Run agent" }));
      expect(screen.getByRole("alert")).toHaveTextContent("You can no longer run Sales Agent.");
      fireEvent.click(screen.getByRole("button", { name: "Do it myself with Shared login" }));
      expect(screen.queryByRole("alert")).toBeNull();
      expect(screen.getByText("Approved")).toBeInTheDocument();
    });

    it("shows the backend message when the offer was already decided", () => {
      m.decide.mockImplementation((_vars: unknown, opts: { onError: (e: Error) => void }) =>
        opts.onError(new ApiError("already decided", 409, { detail: "already decided" })),
      );
      render(<DirectAccessCard approval={withAgents} />);
      fireEvent.click(screen.getByRole("button", { name: "Run agent" }));
      expect(screen.getByRole("alert")).toHaveTextContent("already decided");
    });
  });

  it("looks as before without fitting agents", () => {
    render(
      <DirectAccessCard
        approval={{
          ...offer,
          title: "Copilot on behalf of Anna: read things",
          directAccess: { ...offer.directAccess!, toolKey: "things", label: "things", agents: [] },
        }}
      />,
    );
    expect(screen.getByRole("button", { name: /allow/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Run agent" })).toBeNull();
    expect(screen.queryByText(/Hands over:/)).toBeNull();
    expect(screen.getByText("Copilot on behalf of Anna: read things")).toBeInTheDocument();
  });
});
