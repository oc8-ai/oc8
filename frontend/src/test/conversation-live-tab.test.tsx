// frontend/src/test/conversation-live-tab.test.tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { LiveRunDTO } from "@/lib/conversation-api";

const sendInput = vi.hoisted(() => vi.fn());
const stopRun = vi.hoisted(() => vi.fn());
const conversationProps = vi.hoisted(() => ({ last: null as null | Record<string, unknown> }));
const state = vi.hoisted(() => ({
  manager: true,
  mayCopilot: true,
  runs: [] as unknown[],
  error: null as unknown,
  allRunsCalls: [] as boolean[],
  mayWrite: true,
  stopPending: false,
  steeringAsked: [] as Array<string | null>,
}));
vi.mock("@/components/conversation/conversation", () => ({
  Conversation: (p: Record<string, unknown>) => {
    conversationProps.last = p;
    const extra = (p.extraRuns ?? []) as Array<{
      run: { id: string };
      trigger: React.ReactNode;
      footer?: React.ReactNode;
    }>;
    return (
      <div data-testid="conversation">
        {extra.map((e) => (
          <div key={e.run.id}>
            {e.trigger}
            {e.footer}
          </div>
        ))}
        {p.composerTarget as React.ReactNode}
        {/* The composer's own Stop, as when one of my chat runs answers. */}
        <button type="button" aria-label="Stop" />
        {p.composerTrailing as React.ReactNode}
      </div>
    );
  },
}));
vi.mock("@/lib/governance-hooks", () => ({
  useMayManageAgent: () => () => state.manager,
  useMay: () => () => state.mayCopilot,
}));
vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useLiveTimeline: (_agentId: string, allRuns: boolean) => {
    state.allRunsCalls.push(allRuns);
    if (state.error) return { data: undefined, error: state.error };
    return {
      data: {
        sessionId: "live-1",
        mayViewAllRuns: state.manager,
        nextBefore: null,
        items: state.runs.map((run) => ({
          kind: "run",
          at: (run as LiveRunDTO).createdAt,
          message: null,
          run,
        })),
      },
      error: null,
      hasOlder: false,
      loadingOlder: false,
      fetchOlder: vi.fn(),
    };
  },
  useRunSteering: (runId: string | null) => {
    state.steeringAsked.push(runId);
    return { data: { mayWrite: state.mayWrite, acceptsInputs: true, inputs: [] } };
  },
  useSendRunInput: () => ({ mutate: sendInput }),
  useStopRun: () => ({ mutate: stopRun, isPending: state.stopPending }),
}));

import { cronWords } from "@/components/conversation/cron-words";
import { LiveTimeline, TriggerLine } from "@/components/conversation/live-timeline";
import { ApiError } from "@/lib/api";

function run(over: Partial<LiveRunDTO> = {}): LiveRunDTO {
  return {
    id: "r1",
    state: "running",
    source: "cron",
    createdAt: "2026-11-08T09:00:00Z",
    trigger: { kind: "cron", label: null },
    output: null,
    steps: 1,
    toolCalls: [],
    stepTimings: [],
    startedBy: null,
    redacted: false,
    renderedComponents: [],
    ...over,
  };
}

function wrap(ui: React.ReactElement) {
  return render(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>);
}

const tab = () => <LiveTimeline agentId="a" departmentId="d" promptStarters={[]} />;

beforeEach(() => {
  state.manager = true;
  state.mayCopilot = true;
  state.runs = [run()];
  state.error = null;
  state.allRunsCalls = [];
  state.mayWrite = true;
  state.stopPending = false;
  state.steeringAsked = [];
  sendInput.mockReset();
  stopRun.mockReset();
});

describe("Live tab", () => {
  it("targets the running run and routes the composer's text to it", () => {
    wrap(tab());
    expect(screen.getByText(/To the running run/)).toBeInTheDocument();
    const onSendOverride = conversationProps.last?.onSendOverride as (s: {
      text: string;
      attachmentIds: string[];
      contextRefs: [];
    }) => boolean;
    expect(onSendOverride({ text: "skip ticket 4", attachmentIds: [], contextRefs: [] })).toBe(
      true,
    );
    expect(sendInput).toHaveBeenCalledWith("skip ticket 4", expect.anything());
    expect(conversationProps.last?.sessionId).toBe("live-1");
    expect(conversationProps.last?.daySeparators).toBe(true);
  });

  it("asks for steering only for the running run, never finished or colleague runs (I5)", () => {
    state.runs = [
      run({ id: "old", state: "done", createdAt: "2026-11-08T07:00:00Z" }),
      run({ id: "colleague", redacted: true, createdAt: "2026-11-08T08:00:00Z" }),
      run({ id: "r1" }),
    ];
    wrap(tab());
    expect(new Set(state.steeringAsked.filter(Boolean))).toEqual(new Set(["r1"]));
    // Send stays beside Stop while the composer's text goes to that run.
    expect(conversationProps.last?.sendWhileBusy).toBe(true);
    expect(conversationProps.last?.steeringRunId).toBe("r1");
  });

  it("stops the running run with a Stop of its own, told apart from the composer's", () => {
    wrap(tab());
    const stop = screen.getByRole("button", { name: "Stop run" });
    expect(stop).toHaveTextContent("Stop run");
    expect(stop).toHaveAttribute("title");
    expect(screen.getByRole("button", { name: "Stop" })).not.toBe(stop);
    fireEvent.click(stop);
    expect(stopRun).toHaveBeenCalledWith("r1", expect.anything());
  });

  it("says inline when the run could not be stopped", () => {
    stopRun.mockImplementation((_id: string, opts: { onError: (e: unknown) => void }) =>
      opts.onError(new ApiError("run already finished", 409)),
    );
    wrap(tab());
    fireEvent.click(screen.getByRole("button", { name: "Stop run" }));
    expect(screen.getByRole("alert")).toHaveTextContent("The run has already finished.");
  });

  it("takes no second Stop while one is on its way", () => {
    state.stopPending = true;
    wrap(tab());
    expect(screen.getByRole("button", { name: "Stop run" })).toBeDisabled();
  });

  it("offers no Stop for a run the reader may not write to", () => {
    state.mayWrite = false;
    wrap(tab());
    expect(screen.queryByRole("button", { name: "Stop run" })).toBeNull();
  });

  it("chats as usual while nothing runs", () => {
    state.runs = [run({ state: "done" })];
    wrap(tab());
    expect(screen.queryByText(/To the running run/)).toBeNull();
    const onSendOverride = conversationProps.last?.onSendOverride as (s: {
      text: string;
      attachmentIds: string[];
      contextRefs: [];
    }) => boolean;
    expect(onSendOverride({ text: "hi", attachmentIds: [], contextRefs: [] })).toBe(false);
    expect(sendInput).not.toHaveBeenCalled();
  });

  it("never offers Agent mode: the live tab is not the Copilot's chat", () => {
    state.runs = [run({ state: "done" })];
    wrap(tab());
    expect(conversationProps.last?.agentMode).toBeFalsy();
  });

  it("offers All runs to a manager", () => {
    wrap(tab());
    fireEvent.click(screen.getByRole("switch", { name: /All runs/ }));
    expect(screen.getByRole("switch", { name: /All runs/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(state.allRunsCalls.at(-1)).toBe(true);
  });

  it("never shows All runs or Technical view to anyone else", () => {
    state.manager = false;
    wrap(tab());
    expect(screen.queryByRole("switch", { name: /All runs/ })).toBeNull();
    expect(screen.queryByRole("link", { name: /Technical view/ })).toBeNull();
  });

  it("says inline when All runs is refused", () => {
    state.error = new ApiError("All runs needs the right to manage this agent", 403);
    wrap(tab());
    expect(screen.getByRole("alert")).toHaveTextContent(/may not see all runs/);
  });

  it("links a manager to the run's technical view", () => {
    wrap(tab());
    expect(screen.getByRole("link", { name: /Technical view/ })).toHaveAttribute(
      "href",
      "/agents/a?tab=livelog&runId=r1",
    );
  });

  it("shows a colleague's run as labels only", () => {
    state.runs = [run({ id: "theirs", redacted: true }), run({ id: "mine", state: "done" })];
    wrap(tab());
    const extra = conversationProps.last?.extraRuns as Array<{
      run: { id: string };
      labelsOnly?: boolean;
    }>;
    expect(extra.map((e) => [e.run.id, !!e.labelsOnly])).toEqual([
      ["theirs", true],
      ["mine", false],
    ]);
    // Ruling 15: a colleague's run never leads to its technical view.
    const links = screen.getAllByRole("link", { name: /Technical view/ });
    expect(links.map((a) => a.getAttribute("href"))).toEqual(["/agents/a?tab=livelog&runId=mine"]);
  });

  it("continues a finished run in the Copilot with the run pinned", () => {
    state.runs = [run({ state: "running" }), run({ id: "r2", state: "done" })];
    wrap(tab());
    const links = screen.getAllByRole("link", { name: /Continue/ });
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute("href", "/copilot?pin=run%3Ar2");
  });

  it("offers no Continue without copilot:use, nor on a colleague's redacted run", () => {
    state.runs = [run({ id: "r2", state: "done", redacted: true })];
    wrap(tab());
    expect(screen.queryByRole("link", { name: /Continue/ })).toBeNull();
    state.runs = [run({ id: "r3", state: "done" })];
    state.mayCopilot = false;
    wrap(tab());
    expect(screen.queryByRole("link", { name: /Continue/ })).toBeNull();
    state.mayCopilot = true;
  });
});

describe("trigger lines", () => {
  const line = (over: Partial<LiveRunDTO>) => {
    render(<TriggerLine run={run(over)} />);
  };

  it("writes a webhook in words", () => {
    line({ source: "webhook", trigger: { kind: "webhook", label: "Shop" } });
    expect(screen.getByText("Webhook · Shop")).toBeInTheDocument();
  });

  it("writes a webhook without a name", () => {
    line({ source: "webhook", trigger: { kind: "webhook", label: null } });
    expect(screen.getByText("Webhook")).toBeInTheDocument();
  });

  it("writes a simple schedule as a time", () => {
    line({ trigger: { kind: "cron", label: "0 9 * * *" } });
    expect(screen.getByText("Schedule · daily 09:00")).toBeInTheDocument();
  });

  it("writes an event with its source", () => {
    line({ source: "event", trigger: { kind: "event", label: "github/push" } });
    expect(screen.getByText("Event · github/push")).toBeInTheDocument();
  });

  it("names the delegating agent", () => {
    line({
      source: "delegation",
      trigger: { kind: "delegation", label: "Copilot" },
      startedBy: "Bob",
    });
    expect(screen.getByText("Delegated by Copilot")).toBeInTheDocument();
  });

  it("says a chat started it when it names nobody", () => {
    line({ source: "chat", trigger: { kind: "chat", label: null } });
    expect(screen.getByText("From a chat")).toBeInTheDocument();
  });

  it("names the schedule's zone when it is not the reader's", () => {
    const here = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const other = here === "Europe/Berlin" ? "America/New_York" : "Europe/Berlin";
    line({ trigger: { kind: "cron", label: "0 9 * * *", timezone: other } });
    expect(screen.getByText(`Schedule · daily 09:00 (${other})`)).toBeInTheDocument();
  });

  it("leaves out the zone when it is the reader's", () => {
    const here = Intl.DateTimeFormat().resolvedOptions().timeZone;
    line({ trigger: { kind: "cron", label: "0 9 * * *", timezone: here } });
    expect(screen.getByText("Schedule · daily 09:00")).toBeInTheDocument();
  });

  it("names the member who started it", () => {
    line({ source: "manual", trigger: { kind: "manual", label: "Bob" }, startedBy: "Bob" });
    expect(screen.getByText("From Bob")).toBeInTheDocument();
  });
});

describe("cron words", () => {
  const en = (s: string) => s;
  it("puts simple schedules in words and leaves the rest raw", () => {
    expect(cronWords("0 9 * * *", en)).toBe("daily 09:00");
    expect(cronWords("30 7 * * 1-5", en)).toBe("weekdays 07:30");
    expect(cronWords("*/15 * * * *", en)).toBe("*/15 * * * *");
  });
});
