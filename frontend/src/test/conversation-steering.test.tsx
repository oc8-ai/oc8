// frontend/src/test/conversation-steering.test.tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const steering = vi.hoisted(() => ({ value: undefined as unknown }));
const conv = vi.hoisted(() => ({ turns: [] as unknown[] }));
vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useRunSteering: () => ({ data: steering.value }),
}));
vi.mock("@/components/conversation/use-conversation", async (orig) => ({
  ...(await orig<typeof import("@/components/conversation/use-conversation")>()),
  useConversation: () => ({
    session: undefined,
    messages: [],
    turns: conv.turns,
    isLoading: false,
    liveRunIds: [],
    addPendingRun: vi.fn(),
  }),
}));
vi.mock("@/lib/hooks", async (orig) => ({
  ...(await orig<typeof import("@/lib/hooks")>()),
  useAuth: () => ({ data: { memberId: "me" } }),
  useBudgetStatus: () => ({ data: undefined }),
}));
vi.mock("@/lib/chat-commands", async (orig) => ({
  ...(await orig<typeof import("@/lib/chat-commands")>()),
  useChatModes: () => ({ data: [] }),
}));
vi.mock("@/lib/governance-hooks", () => ({ useCan: () => () => true }));

import { Conversation } from "@/components/conversation/conversation";
import { RunInputs, runInputError, steeringTarget } from "@/components/conversation/run-steering";
import { ApiError } from "@/lib/api";
import type { LiveRunDTO } from "@/lib/conversation-api";
import { applyEvent } from "@/lib/live/apply-event";

const t = (en: string) => en;

function liveRun(over: Partial<LiveRunDTO> = {}): LiveRunDTO {
  return {
    id: "r1",
    state: "done",
    source: "cron",
    createdAt: "2026-11-08T09:00:00Z",
    trigger: { kind: "cron", label: "0 9 * * *" },
    output: "Three tickets triaged.",
    steps: 1,
    toolCalls: [{ tool: "list_tickets", step: 1, state: "done", durationMs: 120 }],
    stepTimings: [],
    startedBy: null,
    ...over,
  };
}

describe("run steering", () => {
  it("targets the running run when the run takes messages", () => {
    const { target, route } = steeringTarget(
      { mayWrite: true, acceptsInputs: true, inputs: [] },
      t,
    );
    render(<>{target}</>);
    expect(route).toBe(true);
    expect(
      screen.getByText("To the running run · taken into account at the next step"),
    ).toBeInTheDocument();
  });

  it("says when a run takes no messages", () => {
    const { target, route } = steeringTarget(
      { mayWrite: true, acceptsInputs: false, inputs: [] },
      t,
    );
    render(<>{target}</>);
    expect(route).toBe(false);
    expect(
      screen.getByText("This run doesn't take messages — you can stop it."),
    ).toBeInTheDocument();
  });

  it("has no target for a run the reader may not write to", () => {
    expect(steeringTarget({ mayWrite: false, acceptsInputs: true, inputs: [] }, t)).toEqual({
      target: null,
      route: false,
    });
    expect(steeringTarget(undefined, t)).toEqual({ target: null, route: false });
  });

  it("shows each input's fate", () => {
    steering.value = {
      mayWrite: false,
      acceptsInputs: true,
      inputs: [
        {
          id: "1",
          text: "use EU",
          authorName: "Alice",
          createdAt: "",
          consumedAt: "x",
          consumedStep: 3,
        },
        {
          id: "2",
          text: "too late",
          authorName: "Alice",
          createdAt: "",
          consumedAt: null,
          consumedStep: null,
        },
      ],
    };
    render(<RunInputs runId="r1" runState="done" />);
    expect(screen.getByText("Taken into account in step 3")).toBeInTheDocument();
    expect(screen.getByText("The run finished before it read this")).toBeInTheDocument();
  });

  it("says an unread input of a running run waits for the next step", () => {
    steering.value = {
      mayWrite: true,
      acceptsInputs: true,
      inputs: [
        {
          id: "1",
          text: "skip 4",
          authorName: "Alice",
          createdAt: "",
          consumedAt: null,
          consumedStep: null,
        },
      ],
    };
    render(<RunInputs runId="r1" runState="running" />);
    expect(screen.getByText("Waiting for the next step")).toBeInTheDocument();
  });

  it("words each refusal for an inline error", () => {
    expect(runInputError(new ApiError("run has already finished", 409), t)).toBe(
      "The run has already finished.",
    );
    expect(runInputError(new ApiError("message is longer than 4000 characters", 422), t)).toBe(
      "Your message is too long (at most 4,000 characters).",
    );
    expect(runInputError(new ApiError("too many", 429), t)).toBe(
      "Too many messages are waiting for this run. Wait until it has read them.",
    );
    expect(runInputError(new Error("boom"), t)).toBe("Your message was not sent. Try again.");
  });

  it("refreshes a run's inputs when the run reads them", () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    applyEvent(qc, {
      id: "e1",
      type: "run.input_consumed",
      data: { run_id: "r1", input_ids: ["1"], step: 3 },
    } as never);
    expect(spy).toHaveBeenCalledWith({ queryKey: ["run-steering", "r1"] });
  });

  it("refreshes the live timeline when a run ends", () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    applyEvent(qc, {
      id: "e2",
      type: "run.status",
      data: { run_id: "r1", state: "done" },
    } as never);
    expect(spy).toHaveBeenCalledWith({ queryKey: ["chat", "live"] });
  });
});

describe("Conversation with timeline runs", () => {
  function wrap(ui: React.ReactElement) {
    return render(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>);
  }

  it("merges runs with the transcript by time, with day separators", () => {
    conv.turns = [
      {
        kind: "user",
        key: "u1",
        message: {
          id: "u1",
          role: "user",
          content: "Hello there",
          createdAt: "2026-11-08T10:00:00Z",
          runId: null,
          renderedComponents: [],
          attachments: [],
          contextRefs: [],
        },
      },
    ];
    wrap(
      <Conversation
        agentId="a"
        sessionId="s"
        onSessionChange={() => undefined}
        variant="full"
        daySeparators
        extraRuns={[
          {
            at: "2026-11-07T09:00:00Z",
            run: liveRun({ id: "old", output: "Yesterday's digest." }),
            trigger: <span>Schedule · 09:00</span>,
          },
          {
            at: "2026-11-08T09:00:00Z",
            run: liveRun(),
            trigger: <span>Schedule · 09:00</span>,
            footer: <span>run footer</span>,
          },
        ]}
      />,
    );
    const column = screen.getByTestId("conversation-scroller");
    const text = column.textContent ?? "";
    expect(text.indexOf("Yesterday's digest.")).toBeLessThan(
      text.indexOf("Three tickets triaged."),
    );
    expect(text.indexOf("Three tickets triaged.")).toBeLessThan(text.indexOf("Hello there"));
    expect(within(column).getAllByRole("separator")).toHaveLength(2);
    expect(screen.getByText("run footer")).toBeInTheDocument();
    expect(screen.getAllByText("Schedule · 09:00")).toHaveLength(2);
  });

  it("shows a colleague's run as step labels only, without details", () => {
    conv.turns = [];
    wrap(
      <Conversation
        agentId="a"
        sessionId="s"
        onSessionChange={() => undefined}
        variant="full"
        emptyState={<p>Nothing yet</p>}
        extraRuns={[
          {
            at: "2026-11-08T09:00:00Z",
            run: liveRun(),
            trigger: <span>From Alice</span>,
            labelsOnly: true,
          },
        ]}
      />,
    );
    expect(screen.queryByText("Nothing yet")).not.toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Steps" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Details/ })).not.toBeInTheDocument();
    expect(screen.getByText("Three tickets triaged.")).toBeInTheDocument();
  });

  it("offers older items while there are more", () => {
    conv.turns = [];
    const onLoadOlder = vi.fn();
    wrap(
      <Conversation
        agentId="a"
        sessionId="s"
        onSessionChange={() => undefined}
        variant="full"
        extraRuns={[{ at: "2026-11-08T09:00:00Z", run: liveRun(), trigger: null }]}
        hasOlder
        onLoadOlder={onLoadOlder}
      />,
    );
    screen.getByRole("button", { name: "Show earlier" }).click();
    expect(onLoadOlder).toHaveBeenCalled();
  });

  it("keeps the reader's place when older items land on top", () => {
    conv.turns = [];
    const qc = new QueryClient();
    const props = {
      agentId: "a",
      sessionId: "s",
      onSessionChange: () => undefined,
      variant: "full" as const,
      hasOlder: true,
      onLoadOlder: vi.fn(),
    };
    const newer = { at: "2026-11-08T09:00:00Z", run: liveRun(), trigger: null };
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <Conversation {...props} extraRuns={[newer]} />
      </QueryClientProvider>,
    );
    const scroller = screen.getByTestId("conversation-scroller");
    let height = 1000;
    Object.defineProperty(scroller, "scrollHeight", { configurable: true, get: () => height });
    scroller.scrollTop = 40;
    screen.getByRole("button", { name: "Show earlier" }).click();
    expect(props.onLoadOlder).toHaveBeenCalledTimes(1);
    height = 1600;
    rerender(
      <QueryClientProvider client={qc}>
        <Conversation
          {...props}
          extraRuns={[
            { at: "2026-11-07T09:00:00Z", run: liveRun({ id: "old" }), trigger: null },
            newer,
          ]}
        />
      </QueryClientProvider>,
    );
    expect(scroller.scrollTop).toBe(640);
  });
});
