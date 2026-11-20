import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

// The notices link through the router (no full page reload). Stub its Link
// as an anchor that renders the href it would navigate to.
vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return {
    ...actual,
    Link: ({
      children,
      to,
      params,
      search,
    }: {
      children?: ReactNode;
      to: string;
      params?: Record<string, string>;
      search?: Record<string, string>;
    }) => {
      const path = to.replace(/\$(\w+)/g, (_, key: string) => params?.[key] ?? "");
      const query = search ? `?${new URLSearchParams(search).toString()}` : "";
      return <a href={`${path}${query}`}>{children}</a>;
    },
  };
});
vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useTurnRun: () => ({ data: undefined }),
  useMessageFeedback: () => ({ mutate: vi.fn() }),
}));
vi.mock("@/lib/governance-hooks", async (orig) => ({
  ...(await orig<typeof import("@/lib/governance-hooks")>()),
  useMay: () => (permission: string) => permission === "department:manage",
  useMayManageAgent: () => () => false,
}));

import { AssistantTurn } from "@/components/conversation/assistant-turn";
import type { RunActivityDTO } from "@/lib/hooks-chat";

const RUN: RunActivityDTO = {
  id: "r1",
  agentId: "a1",
  state: "running",
  phase: null,
  output: null,
  steps: 0,
  toolCalls: [],
  taskId: null,
  question: null,
  renderedComponents: [],
};

function renderTurn(run: RunActivityDTO) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <AssistantTurn
        sessionId="s"
        turn={{ kind: "assistant", key: "k", replyToId: "u1", variants: [], liveRunId: "r1" }}
        run={run}
      />
    </QueryClientProvider>,
  );
}

describe("AssistantTurn run hints", () => {
  it("says the workspace is starting before the first step", () => {
    renderTurn({ ...RUN, phase: "provisioning" });
    expect(screen.getByText("Starting the workspace …")).toBeInTheDocument();
    expect(screen.queryByText(/provisioning/)).toBeNull();
  });

  it("drops the start line once the run has ended", () => {
    renderTurn({ ...RUN, state: "failed", phase: "starting" });
    expect(screen.queryByText("Starting the workspace …")).toBeNull();
  });

  it("says which tool the department withholds and links to its guardrails", () => {
    renderTurn({
      ...RUN,
      state: "done",
      output: "done",
      steps: 1,
      toolDenied: {
        connection: "microsoft365",
        tool: "mail_search",
        label: "Microsoft 365",
        right: "read",
        reason: "tool_not_in_frame",
        scope: "department",
        departmentId: "d1",
        agentId: "a1",
      },
    });
    expect(
      screen.getByText(
        "This department does not allow the tool “mail_search” (Microsoft 365). An admin can enable it under Department → Guardrails.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open guardrails →" })).toHaveAttribute(
      "href",
      "/departments/d1?tab=guardrails",
    );
  });
});
