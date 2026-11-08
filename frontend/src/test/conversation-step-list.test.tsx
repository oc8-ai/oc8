import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/tool-labels", async (orig) => ({
  ...(await orig<typeof import("@/lib/tool-labels")>()),
  useToolLabels: () => ({ data: undefined }),
}));

import { StepList } from "@/components/conversation/step-list";
import { toolKind } from "@/components/conversation/tool-previews";

function wrap(ui: React.ReactElement) {
  return render(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>);
}

const run = {
  state: "done",
  steps: 4,
  toolCalls: [
    {
      tool: "web_search",
      step: 1,
      arguments: { query: "vat rates" },
      result: JSON.stringify([{ title: "VAT 2026", url: "https://gov.test/vat" }]),
      state: "done",
    },
    {
      tool: "search_knowledge",
      step: 2,
      arguments: { query: "refunds" },
      result: "[Knowledge: Handbook]\n- 30 days (source: kb://handbook)",
      state: "done",
    },
    {
      tool: "delegate_task",
      step: 3,
      arguments: { agent_id: "a2", task_text: "check stock" },
      result: "Handed to Stock agent",
      state: "done",
    },
    {
      tool: "list_orders",
      step: 4,
      connection: "odoo",
      arguments: { limit: 5 },
      result: "[]",
      state: "done",
      durationMs: 820,
    },
  ],
  stepTimings: [{ step: 4, tokensIn: 100, tokensOut: 20 }],
};

describe("StepList", () => {
  it("classifies each tool into a preview kind", () => {
    expect(
      ["web_search", "fetch_url", "search_knowledge", "read_context", "delegate_task", "x"].map(
        (tool) => toolKind({ tool, connection: tool === "x" ? "odoo" : null } as never),
      ),
    ).toEqual(["web", "web", "knowledge", "file", "delegation", "connection"]);
  });

  it("renders one line per step with a web preview and raw details behind Details", () => {
    wrap(<StepList run={run} />);
    expect(screen.getByText("gov.test")).toBeInTheDocument();
    expect(screen.getByText(/Handed to/)).toBeInTheDocument();
    const details = screen.getAllByRole("button", { name: /Details/ });
    expect(details).toHaveLength(4);
    fireEvent.click(details[3]);
    expect(screen.getByText(/"limit": 5/)).toBeInTheDocument();
    expect(screen.getByText(/100 → 20 tokens/)).toBeInTheDocument();
  });

  it("renders nothing for a run without tool calls", () => {
    const { container } = wrap(<StepList run={{ state: "done", steps: 1, toolCalls: [] }} />);
    expect(container).toBeEmptyDOMElement();
  });
});
