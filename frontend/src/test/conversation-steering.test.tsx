// frontend/src/test/conversation-steering.test.tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const steering = vi.hoisted(() => ({ value: undefined as unknown }));
const conv = vi.hoisted(() => ({ turns: [] as unknown[] }));
const watched = vi.hoisted(() => ({ ids: [] as string[] }));
vi.mock("@/lib/live/provider", async (orig) => ({
  ...(await orig<typeof import("@/lib/live/provider")>()),
  useWatchRuns: (ids: readonly string[]) => {
    watched.ids.push(...ids);
  },
}));
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
vi.mock("@/lib/governance-hooks", () => ({
  useCallerCapabilities: () => null,
  useCan: () => () => true,
  // No approval:view: the chat sends no approvals request.
  useMay: () => () => false,
}));

import { Conversation } from "@/components/conversation/conversation";
import { RunInputs, runInputError, steeringTarget } from "@/components/conversation/run-steering";
import { ApiError } from "@/lib/api";
import { pickLive, useTimelineRuns } from "@/components/conversation/timeline-runs";
import {
  flattenLivePages,
  liveTimelineRefetchInterval,
  type LiveRunDTO,
  type LiveTimelineDTO,
} from "@/lib/conversation-api";
import type { RunActivityDTO } from "@/lib/hooks-chat";
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
    redacted: false,
    renderedComponents: [],
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
    expect(
      runInputError(new ApiError("I cannot accept, request, or use credential values.", 422), t),
    ).toBe("I cannot accept, request, or use credential values.");
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

  it("watches only my running timeline runs, never a colleague's", () => {
    conv.turns = [];
    watched.ids = [];
    wrap(
      <Conversation
        agentId="a"
        sessionId="s"
        onSessionChange={() => undefined}
        variant="full"
        extraRuns={[
          {
            at: "2026-11-08T09:00:00Z",
            run: liveRun({ id: "mine", state: "running" }),
            trigger: null,
          },
          {
            at: "2026-11-08T09:01:00Z",
            run: liveRun({ id: "theirs", state: "running" }),
            trigger: null,
            labelsOnly: true,
          },
          { at: "2026-11-08T09:02:00Z", run: liveRun({ id: "ended" }), trigger: null },
        ]}
      />,
    );
    expect(watched.ids).toContain("mine");
    expect(watched.ids).not.toContain("theirs");
    expect(watched.ids).not.toContain("ended");
  });

  it("shows a run that is also a chat turn only once", () => {
    conv.turns = [
      {
        kind: "user",
        key: "u1",
        message: {
          id: "u1",
          role: "user",
          content: "Hello there",
          createdAt: "2026-11-08T08:00:00Z",
          runId: null,
          renderedComponents: [],
          attachments: [],
          contextRefs: [],
        },
      },
      { kind: "assistant", key: "answer-u1", replyToId: "u1", variants: [], liveRunId: "live-run" },
      {
        kind: "assistant",
        key: "a-old",
        replyToId: null,
        variants: [
          {
            id: "a-old",
            role: "assistant",
            content: "Earlier answer",
            createdAt: "2026-11-08T07:00:00Z",
            runId: "done-run",
            renderedComponents: [],
            attachments: [],
            contextRefs: [],
          },
        ],
        liveRunId: null,
      },
    ];
    wrap(
      <Conversation
        agentId="a"
        sessionId="s"
        onSessionChange={() => undefined}
        variant="full"
        extraRuns={[
          {
            at: "2026-11-08T08:00:01Z",
            run: liveRun({ id: "live-run", state: "running" }),
            trigger: <span>dup live</span>,
          },
          {
            at: "2026-11-08T07:00:00Z",
            run: liveRun({ id: "done-run" }),
            trigger: <span>dup done</span>,
          },
          { at: "2026-11-08T09:00:00Z", run: liveRun({ id: "cron" }), trigger: <span>kept</span> },
        ]}
      />,
    );
    expect(screen.queryByText("dup live")).not.toBeInTheDocument();
    expect(screen.queryByText("dup done")).not.toBeInTheDocument();
    expect(screen.getByText("kept")).toBeInTheDocument();
  });

  it("puts an earlier offset time first even when its string sorts later", () => {
    conv.turns = [];
    wrap(
      <Conversation
        agentId="a"
        sessionId="s"
        onSessionChange={() => undefined}
        variant="full"
        extraRuns={[
          {
            at: "2026-11-08T07:30:00Z",
            run: liveRun({ id: "b", output: "Second" }),
            trigger: null,
          },
          {
            at: "2026-11-08T09:00:00+02:00",
            run: liveRun({ id: "a", output: "First" }),
            trigger: null,
          },
        ]}
      />,
    );
    const text = screen.getByTestId("conversation-scroller").textContent ?? "";
    expect(text.indexOf("First")).toBeLessThan(text.indexOf("Second"));
  });
});

describe("timeline helpers", () => {
  const page = (
    items: string[],
    nextBefore: string | null,
    extra: Partial<LiveTimelineDTO> = {},
  ) => ({
    sessionId: "live-1",
    mayViewAllRuns: true,
    nextBefore,
    items: items.map((id) => ({
      kind: "run" as const,
      at: id,
      message: null,
      run: liveRun({ id }),
    })),
    ...extra,
  });

  it("flattens pages oldest first and keeps the oldest page's cursor", () => {
    const flat = flattenLivePages({
      pages: [page(["c", "d"], "cur-1"), page(["a", "b"], null, { mayViewAllRuns: false })],
      pageParams: [null, "cur-1"],
    });
    expect(flat.items.map((i) => i.run?.id)).toEqual(["a", "b", "c", "d"]);
    expect(flat.nextBefore).toBeNull();
    expect(flat.mayViewAllRuns).toBe(true);
    expect(flat.sessionId).toBe("live-1");
  });

  it("polls every 10 s only while a colleague's run is running", () => {
    const isColleague = (run: LiveRunDTO) => run.id.startsWith("c-");
    const data = (runs: LiveRunDTO[]) => ({
      pages: [
        {
          sessionId: "s",
          mayViewAllRuns: true,
          nextBefore: null,
          items: runs.map((run) => ({
            kind: "run" as const,
            at: run.createdAt,
            message: null,
            run,
          })),
        },
      ],
      pageParams: [null],
    });
    expect(
      liveTimelineRefetchInterval(data([liveRun({ id: "c-1", state: "running" })]), isColleague),
    ).toBe(10_000);
    expect(
      liveTimelineRefetchInterval(data([liveRun({ id: "c-1", state: "done" })]), isColleague),
    ).toBe(false);
    expect(
      liveTimelineRefetchInterval(data([liveRun({ id: "mine", state: "running" })]), isColleague),
    ).toBe(false);
    expect(liveTimelineRefetchInterval(undefined, isColleague)).toBe(false);
  });

  it("counts a run as a colleague's when the server redacted it", () => {
    const data = (run: LiveRunDTO) => ({
      pages: [
        {
          sessionId: "s",
          mayViewAllRuns: true,
          nextBefore: null,
          items: [{ kind: "run" as const, at: run.createdAt, message: null, run }],
        },
      ],
      pageParams: [null],
    });
    expect(liveTimelineRefetchInterval(data(liveRun({ state: "running", redacted: true })))).toBe(
      10_000,
    );
    expect(liveTimelineRefetchInterval(data(liveRun({ state: "running" })))).toBe(false);
  });

  it("takes from the live cache only what the patchers fill", () => {
    const cached = {
      id: "r",
      agentId: "a",
      state: "running",
      phase: "tool",
      output: null,
      steps: 0,
      toolCalls: [],
      stepTimings: [],
      taskId: null,
      question: null,
      renderedComponents: [],
    } as RunActivityDTO;
    expect(pickLive(cached)).toEqual({ state: "running", phase: "tool" });
    expect(
      pickLive({ ...cached, steps: 2, toolCalls: [{ tool: "x" }], stepTimings: [{ step: 1 }] }),
    ).toEqual({
      state: "running",
      phase: "tool",
      steps: 2,
      toolCalls: [{ tool: "x" }],
      stepTimings: [{ step: 1 }],
    });
  });

  it("uses a colleague's run as the timeline has it, never a stale live cache", () => {
    const qc = new QueryClient();
    qc.setQueryData(["run", "theirs"], {
      id: "theirs",
      state: "queued",
      steps: 0,
      toolCalls: [],
      components: [{ componentKey: "table", props: {} }],
    });
    let result: Record<string, RunActivityDTO> = {};
    function Probe() {
      result = useTimelineRuns(
        [liveRun({ id: "theirs", state: "running", steps: 3, redacted: true })],
        "a",
        new Set(),
      );
      return null;
    }
    render(
      <QueryClientProvider client={qc}>
        <Probe />
      </QueryClientProvider>,
    );
    expect(result.theirs?.state).toBe("running");
    expect(result.theirs?.steps).toBe(3);
    expect(result.theirs?.components).toEqual([]);
  });

  it("keeps the cards a watched run rendered once it has finished", async () => {
    const qc = new QueryClient();
    let result: Record<string, RunActivityDTO> = {};
    function Probe({ run, watched }: { run: LiveRunDTO; watched: string[] }) {
      result = useTimelineRuns([run], "a", new Set(watched));
      return null;
    }
    const ui = (run: LiveRunDTO, watched: string[]) => (
      <QueryClientProvider client={qc}>
        <Probe run={run} watched={watched} />
      </QueryClientProvider>
    );
    const view = render(ui(liveRun({ state: "running" }), ["r1"]));
    await act(async () =>
      applyEvent(qc, {
        id: "e3",
        type: "run.component_rendered",
        data: { run_id: "r1", component_key: "table", props: { title: "Inbox" } },
      } as never),
    );
    await waitFor(() =>
      expect(result.r1?.components).toEqual([{ componentKey: "table", props: { title: "Inbox" } }]),
    );
    view.rerender(ui(liveRun({ state: "done" }), []));
    expect(result.r1?.components).toEqual([{ componentKey: "table", props: { title: "Inbox" } }]);
  });

  it("shows the cards a finished run carries on the timeline", () => {
    let result: Record<string, RunActivityDTO> = {};
    const card = { componentKey: "table", props: { title: "Inbox" } };
    function Probe() {
      result = useTimelineRuns([liveRun({ renderedComponents: [card] })], "a", new Set());
      return null;
    }
    render(
      <QueryClientProvider client={new QueryClient()}>
        <Probe />
      </QueryClientProvider>,
    );
    expect(result.r1?.components).toEqual([card]);
  });
});
