import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ApprovalPane } from "@/components/dashboard/widgets/approvals-widget";
import type { Approval } from "@/lib/hooks";

const BASE: Approval = {
  id: "a1",
  agentId: "agent-1",
  agentName: "Nora",
  title: "Nora wants to create a quotation",
  detail: "over 3000 EUR",
  amount: "4.200,00 €",
  time: "",
  status: "pending",
  actionType: "tool_send",
  toolName: "create_record",
  toolArguments: { model: "sale.order" },
};

function renderPane(approval: Approval) {
  return render(
    <ApprovalPane
      approval={approval}
      mayAct={false}
      viewOnlyBecause="role"
      busy={false}
      onDecide={() => {}}
    />,
  );
}

describe("the approval pane's record link", () => {
  it("links straight to the record when the approval carries a URL", () => {
    renderPane({ ...BASE, recordUrl: "https://odoo.example.com/odoo/sale.order/42" });
    const link = screen.getByRole("link", { name: /open the record/i });
    expect(link).toHaveAttribute("href", "https://odoo.example.com/odoo/sale.order/42");
    // The href comes from a tool call's own arguments -- the opened page must
    // not get a handle on this window.
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));
  });

  it("shows no link at all when the approval carries none", () => {
    renderPane(BASE);
    expect(screen.queryByRole("link", { name: /open the record/i })).toBeNull();
  });

  it("still renders the approval itself without a link", () => {
    renderPane(BASE);
    expect(screen.getByText("Nora wants to create a quotation")).toBeInTheDocument();
  });
});
