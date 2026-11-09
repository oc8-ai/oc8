// frontend/src/test/conversation-guardrail-card.test.tsx
// Plan Task 43: PR #71's member-guardrail cards show inside the turn once it
// lands, with no change to the conversation. Its two cards are stood in for
// here, registered exactly the way #71 registers them:
//   - `guardrail_card` (a rendered component) as a RUN_COMPONENT_REGISTRY key;
//   - `ChatApprovalCard` as a TURN_APPROVAL_CARDS entry fed by its own
//     `useMyApprovals({ poll })`.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const conv = vi.hoisted(() => ({ current: {} as Record<string, unknown> }));
const mine = vi.hoisted(() => ({ current: [] as unknown[], polls: [] as unknown[] }));

vi.mock("@/lib/hooks", async (orig) => ({
  ...(await orig<typeof import("@/lib/hooks")>()),
  useDecideApproval: () => ({ mutate: vi.fn(), isPending: false }),
  // Core's own approval list stays empty: #71's cards come from its own source.
  useApprovals: () => ({ data: [] }),
  useAuth: () => ({ data: { memberId: "me" } }),
  useBudgetStatus: () => ({ data: undefined }),
}));
vi.mock("@/components/conversation/use-conversation", () => ({
  useConversation: () => ({ ...conv.current, isLoading: false, addPendingRun: vi.fn() }),
}));
vi.mock("@/lib/chat-commands", async (orig) => ({
  ...(await orig<typeof import("@/lib/chat-commands")>()),
  useChatModes: () => ({ data: [] }),
}));
vi.mock("@/lib/governance-hooks", () => ({
  useCallerCapabilities: () => null,
  useCan: () => () => true,
  useMay: () => () => true,
}));

import { Conversation } from "@/components/conversation";
import { AssistantTurn } from "@/components/conversation/assistant-turn";
import { TURN_APPROVAL_CARDS } from "@/components/conversation/turn-approval-registry";
import { RUN_COMPONENT_REGISTRY } from "@/components/run-record-card";
import type { Approval } from "@/lib/hooks";
import type { ChatMessageDTO } from "@/lib/hooks-chat";

function message(over: Partial<ChatMessageDTO>): ChatMessageDTO {
  return {
    id: "m",
    sessionId: "s1",
    role: "assistant",
    content: "",
    runId: null,
    renderedComponents: [],
    createdAt: "2026-11-09T10:00:00Z",
    attachments: [],
    mode: null,
    contextRefs: [],
    authorMemberId: null,
    authorName: null,
    replyToId: null,
    ...over,
  };
}

/** Stand-in for #71's `GuardrailCard`: reads the props its real one reads. */
function StandInGuardrailCard({ props }: { props: Record<string, unknown> }) {
  return <div>{`Suggested rule: ${String(props.toolKey)} · ${String(props.level)}`}</div>;
}

/** Stand-in for #71's `ChatApprovalCard` ({ approval, onChangeRule? }). */
function StandInChatApprovalCard({ approval }: { approval: Approval; onChangeRule?: () => void }) {
  return <div data-testid="member-approval">{approval.title}</div>;
}

/** Stand-in for #71's `useMyApprovals({ poll })`: a query result. */
function useStandInMyApprovals({ poll = false }: { poll?: number | false } = {}) {
  mine.polls.push(poll);
  return { data: mine.current as Approval[] };
}

afterEach(() => {
  delete RUN_COMPONENT_REGISTRY.guardrail_card;
  delete TURN_APPROVAL_CARDS.member_guardrail;
  mine.current = [];
  mine.polls = [];
});

describe("PR #71's guardrail_card in a turn", () => {
  it("resolves through RUN_COMPONENT_REGISTRY, the registry #71 extends", () => {
    RUN_COMPONENT_REGISTRY.guardrail_card = StandInGuardrailCard;
    const answer = message({
      id: "a1",
      content: "Soll ich mir das merken?",
      runId: "r1",
      replyToId: "u1",
      renderedComponents: [
        {
          componentKey: "guardrail_card",
          props: { agentId: "a", toolKey: "gmail", level: "ask_first" },
        },
      ],
    });
    render(
      <QueryClientProvider client={new QueryClient()}>
        <AssistantTurn
          sessionId="s1"
          turn={{
            kind: "assistant",
            key: "k",
            replyToId: "u1",
            variants: [answer],
            liveRunId: null,
          }}
        />
      </QueryClientProvider>,
    );
    expect(screen.getByText("Suggested rule: gmail · ask_first")).toBeInTheDocument();
  });
});

function approval(over: Partial<Approval>): Approval {
  return {
    id: "ap",
    agentId: "a",
    title: "",
    detail: "",
    amount: null,
    time: "",
    status: "pending",
    actionType: "tool_send",
    ...over,
  };
}

function twoTurns({ secondLive }: { secondLive: boolean }) {
  const u1 = message({ id: "u1", role: "user", content: "First", authorMemberId: "me" });
  const a1 = message({ id: "a1", content: "Old answer", replyToId: "u1", runId: "r1" });
  const u2 = message({ id: "u2", role: "user", content: "Second", authorMemberId: "me" });
  const a2 = message({ id: "a2", content: "New answer", replyToId: "u2", runId: "r2" });
  conv.current = {
    session: {
      id: "s1",
      agentId: "a",
      modelOverrideId: null,
      kind: "chat",
      activeRuns: [],
      readOnly: false,
      pendingQuestion: null,
      taskId: "task-1",
    },
    messages: [u1, a1, u2, a2],
    turns: [
      { kind: "user", key: "u1", message: u1 },
      { kind: "assistant", key: "a1", replyToId: "u1", variants: [a1], liveRunId: null },
      { kind: "user", key: "u2", message: u2 },
      secondLive
        ? { kind: "assistant", key: "a2", replyToId: "u2", variants: [], liveRunId: "r2" }
        : { kind: "assistant", key: "a2", replyToId: "u2", variants: [a2], liveRunId: null },
    ],
    liveRunIds: secondLive ? ["r2"] : [],
  };
}

function mount() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <Conversation agentId="a" sessionId="s1" onSessionChange={vi.fn()} variant="full" />
    </QueryClientProvider>,
  );
}

describe("PR #71's ChatApprovalCard in a turn", () => {
  it("shows each approval in the turn whose run it holds, decisions excepted", () => {
    TURN_APPROVAL_CARDS.member_guardrail = {
      useApprovals: useStandInMyApprovals,
      Card: StandInChatApprovalCard,
    };
    mine.current = [
      approval({ id: "ap1", runId: "r1", title: "Send mail to kunde@x.de" }),
      approval({ id: "ap2", runId: "r2", title: "Delete order 4711" }),
      approval({ id: "ap3", runId: "r2", title: "A decision", actionType: "decision" }),
      approval({ id: "ap4", runId: "r9", title: "Another chat's run" }),
    ];
    twoTurns({ secondLive: false });
    mount();
    const [first, second] = screen.getAllByRole("article", { name: "Answer" });
    expect(within(first).getByTestId("member-approval")).toHaveTextContent(
      "Send mail to kunde@x.de",
    );
    expect(within(second).getByTestId("member-approval")).toHaveTextContent("Delete order 4711");
    expect(screen.getAllByTestId("member-approval")).toHaveLength(2);
    expect(screen.queryByText("A decision")).toBeNull();
    expect(screen.queryByText("Another chat's run")).toBeNull();
  });

  it("keeps a card once decided and looks again only while the run is live", () => {
    TURN_APPROVAL_CARDS.member_guardrail = {
      useApprovals: useStandInMyApprovals,
      Card: StandInChatApprovalCard,
    };
    mine.current = [approval({ id: "ap2", runId: "r2", title: "Delete order 4711" })];
    twoTurns({ secondLive: true });
    const view = mount();
    expect(screen.getByText("Delete order 4711")).toBeInTheDocument();
    expect(mine.polls).toContain(3000);
    // Decided: it leaves the pending list, the card stays.
    mine.current = [];
    view.rerender(
      <QueryClientProvider client={new QueryClient()}>
        <Conversation agentId="a" sessionId="s1" onSessionChange={vi.fn()} variant="full" />
      </QueryClientProvider>,
    );
    expect(screen.getByText("Delete order 4711")).toBeInTheDocument();
  });

  it("renders nothing extra while nobody registered a card", () => {
    mine.current = [approval({ id: "ap1", runId: "r1", title: "Send mail to kunde@x.de" })];
    twoTurns({ secondLive: false });
    mount();
    expect(screen.queryByTestId("member-approval")).toBeNull();
    expect(mine.polls).toEqual([]);
  });
});
