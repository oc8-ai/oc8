import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

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
});
