import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useTurnRun: () => ({ data: undefined }),
  useMessageFeedback: () => ({ mutate: vi.fn() }),
}));

import { AssistantTurn } from "@/components/conversation/assistant-turn";
import type { RunActivityDTO } from "@/lib/hooks-chat";

function renderLiveTurn(state: string) {
  const run = {
    id: "r1",
    agentId: "a1",
    state,
    output: "",
    liveAnswer: "",
    steps: [],
    toolCalls: [],
    components: [],
  } as unknown as RunActivityDTO;
  render(
    <QueryClientProvider client={new QueryClient()}>
      <AssistantTurn
        sessionId="s"
        run={run}
        turn={{ kind: "assistant", key: "a", replyToId: "u1", variants: [], liveRunId: "r1" }}
      />
    </QueryClientProvider>,
  );
}

describe("a turn that was stopped (E2E N7)", () => {
  it("says it was stopped instead of ending silently", () => {
    renderLiveTurn("interrupted");
    expect(screen.getByRole("status")).toHaveTextContent(
      "Stopped. Nothing more was done in this turn.",
    );
  });

  it("says nothing of the kind for a turn that finished", () => {
    renderLiveTurn("done");
    expect(screen.queryByText(/Stopped\./)).toBeNull();
  });
});
