import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ navigate: vi.fn() }));
vi.mock("@tanstack/react-router", () => ({ useRouter: () => ({ navigate: nav.navigate }) }));

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

  it("never makes the browser contact a host a tool call named", () => {
    const { container } = wrap(<StepList run={run} />);
    expect(screen.getByText("gov.test")).toBeInTheDocument();
    const external = [...container.querySelectorAll("img")].filter((img) =>
      /^(https?:)?\/\//.test(img.getAttribute("src") ?? ""),
    );
    expect(external).toEqual([]);
  });

  it("opens a delegated agent in-app and gives Details a 36px hit area", () => {
    wrap(<StepList run={run} />);
    const link = screen.getByRole("link", { name: /Handed to/ });
    expect(link).toHaveAttribute("href", "/agents/a2");
    fireEvent.click(link, { button: 0 });
    expect(nav.navigate).toHaveBeenCalledWith({ href: "/agents/a2" });
    for (const button of screen.getAllByRole("button", { name: /Details/ })) {
      expect(button).toHaveClass("min-h-9");
    }
  });

  it("shows a question to the reader in the neutral colour, a refused call in red (E2E G)", () => {
    wrap(
      <StepList
        run={{
          state: "done",
          steps: 2,
          toolCalls: [
            { tool: "ask_user", step: 1, arguments: {}, result: "", state: "denied" },
            { tool: "list_orders", step: 2, arguments: {}, result: "", state: "denied" },
          ],
        }}
      />,
    );
    const asked = screen.getByText("Asked you").closest("span.flex-1");
    expect(asked?.className).not.toContain("text-status-error");
    const items = screen.getAllByRole("listitem");
    expect(items[1].querySelector("span.flex-1")?.className).toContain("text-status-error");
  });

  it("says once that a knowledge base could not be searched with this model (E2E H)", () => {
    const line = "[Skipped: a knowledge base \u2013 needs a local embedding model]";
    wrap(
      <StepList
        run={{
          state: "done",
          steps: 2,
          toolCalls: [1, 2].map((step) => ({
            tool: "search_knowledge",
            step,
            arguments: { query: "q" },
            result: `${line}\nDazu steht nichts in der Wissensdatenbank.`,
            state: "done",
          })),
        }}
      />,
    );
    expect(
      screen.getAllByText(
        "A knowledge base can't be searched with this model – it needs a local embedding model.",
      ),
    ).toHaveLength(1);
  });

  it("renders nothing for a run without tool calls", () => {
    const { container } = wrap(<StepList run={{ state: "done", steps: 1, toolCalls: [] }} />);
    expect(container).toBeEmptyDOMElement();
  });
});
