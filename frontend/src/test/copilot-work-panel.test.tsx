import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const m = vi.hoisted(() => ({
  setState: vi.fn(),
  endFollowup: vi.fn(),
  deleteNote: vi.fn(),
  pause: vi.fn(),
  resume: vi.fn(),
  cancelRun: vi.fn(),
  agentId: "agent-1" as string | undefined,
}));

vi.mock("@/lib/hooks-copilot", () => ({
  copilotKeys: { delegations: ["copilot", "delegations"] },
  useCopilotProfile: () => ({
    data: {
      displayName: "Copilot",
      avatar: { shape: "round", color: "indigo" },
      pausedAt: null,
      status: "ready",
      activeCount: 0,
      agentId: m.agentId,
    },
  }),
  usePauseCopilot: () => ({ mutate: m.pause, isPending: false }),
  useResumeCopilot: () => ({ mutate: m.resume, isPending: false }),
  useResponsibilities: () => ({
    data: [
      {
        id: "r1",
        title: "Watch the invoice",
        goal: "Tell me when it is paid",
        state: "active",
        nextStep: "Check the bank feed",
        notifyRule: "risks_and_decisions",
        originChannel: null,
        lastUpdateAt: null,
        createdAt: "2026-10-01T00:00:00Z",
        chatSessionId: "s1",
      },
    ],
  }),
  useSetResponsibilityState: () => ({ mutate: m.setState, isPending: false }),
  useFollowups: () => ({
    data: [
      {
        id: "f1",
        responsibilityId: "r1",
        responsibilityTitle: "Watch the invoice",
        kind: "cron",
        cronExpression: "0 9 * * *",
        timezone: "Europe/Berlin",
        nextRunAt: "2026-10-05T07:00:00Z",
        endsAt: "2026-11-01T00:00:00Z",
        enabled: true,
        lastSkipReason: null,
      },
    ],
  }),
  useEndFollowup: () => ({ mutate: m.endFollowup, isPending: false }),
  useCopilotDelegations: () => ({
    data: [{ id: "run-1", agentId: "x", state: "running", steps: 1, toolCalls: [] }],
  }),
  useCopilotNotes: () => ({
    data: [{ id: "n1", content: "Prefers short answers", createdAt: "2026-10-01T00:00:00Z" }],
  }),
  useDeleteCopilotNote: () => ({ mutate: m.deleteNote, isPending: false }),
}));

vi.mock("@/lib/hooks", () => ({
  useCancelRun: () => ({ mutate: m.cancelRun, isPending: false }),
}));

vi.mock("@/components/run-step-timeline", () => ({
  RunStepTimeline: () => <div data-testid="timeline" />,
}));

vi.mock("@/components/dashboard/widgets/needs-me-widget", () => ({
  NeedsMeWidget: ({ agentId }: { agentId?: string }) => <div data-testid="needs-me">{agentId}</div>,
}));

import { CopilotPersonaHeader } from "@/components/copilot-persona";
import { CopilotWorkPanel } from "@/components/copilot-work-panel";

function wrap(ui: React.ReactNode) {
  return render(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>);
}

function openTab(name: RegExp) {
  fireEvent.mouseDown(screen.getByRole("tab", { name }), { button: 0 });
}

beforeEach(() => {
  vi.clearAllMocks();
  m.agentId = "agent-1";
});

describe("CopilotWorkPanel", () => {
  it("activity tab shows the responsibility and pauses / completes it", () => {
    wrap(<CopilotWorkPanel />);
    expect(screen.getByText("Watch the invoice")).toBeInTheDocument();
    expect(screen.getByText("Tell me when it is paid")).toBeInTheDocument();
    expect(screen.getByText(/Check the bank feed/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    expect(m.setState).toHaveBeenCalledWith({ id: "r1", state: "paused" });
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(m.setState).toHaveBeenCalledWith({ id: "r1", state: "done" });
  });

  it("scheduled tab shows zone and end, and ends the schedule", () => {
    wrap(<CopilotWorkPanel />);
    openTab(/scheduled/i);
    expect(screen.getByText(/Europe\/Berlin/)).toBeInTheDocument();
    expect(screen.getByText(/Ends:/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "End schedule" }));
    expect(m.endFollowup).toHaveBeenCalledWith("f1");
  });

  it("the three stop controls call three different things", () => {
    wrap(
      <>
        <CopilotPersonaHeader variant="page" />
        <CopilotWorkPanel />
      </>,
    );
    // Page pause (header) -- the panel's own "Pause" is per-responsibility.
    fireEvent.click(screen.getAllByRole("button", { name: "Pause" })[0]);
    expect(m.pause).toHaveBeenCalledTimes(1);
    expect(m.cancelRun).not.toHaveBeenCalled();
    expect(m.endFollowup).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(m.cancelRun).toHaveBeenCalledTimes(1);
    expect(m.pause).toHaveBeenCalledTimes(1);
    expect(m.endFollowup).not.toHaveBeenCalled();

    openTab(/scheduled/i);
    fireEvent.click(screen.getByRole("button", { name: "End schedule" }));
    expect(m.endFollowup).toHaveBeenCalledTimes(1);
    expect(m.pause).toHaveBeenCalledTimes(1);
    expect(m.cancelRun).toHaveBeenCalledTimes(1);
    expect(m.setState).not.toHaveBeenCalled();
  });

  it("waiting tab scopes the queue to the Copilot agent", () => {
    wrap(<CopilotWorkPanel />);
    openTab(/waiting/i);
    expect(screen.getByTestId("needs-me")).toHaveTextContent("agent-1");
  });

  it("notes tab deletes a note", () => {
    wrap(<CopilotWorkPanel />);
    openTab(/notes/i);
    expect(screen.getByText(/Only you can see them/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(m.deleteNote).toHaveBeenCalledWith("n1");
  });

  it("waiting tab never renders the unscoped queue before the profile is known", () => {
    m.agentId = undefined;
    wrap(<CopilotWorkPanel />);
    openTab(/waiting/i);
    expect(screen.queryByTestId("needs-me")).toBeNull();
  });

  it("cancelling a responsibility asks first, then calls the mutation once", async () => {
    wrap(<CopilotWorkPanel />);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(m.setState).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: "Cancel responsibility" }));
    expect(m.setState).toHaveBeenCalledTimes(1);
    expect(m.setState).toHaveBeenCalledWith({ id: "r1", state: "cancelled" });
  });

  it("a successful run Stop refreshes the delegations list", () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    m.cancelRun.mockImplementation((_v: unknown, o: { onSuccess: () => void }) => o.onSuccess());
    render(
      <QueryClientProvider client={qc}>
        <CopilotWorkPanel />
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(spy).toHaveBeenCalledWith({ queryKey: ["copilot", "delegations"] });
  });
});
