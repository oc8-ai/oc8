// frontend/src/test/agent-private-content-notice.test.tsx
// A personal agent read by someone other than its owner comes back with
// `contentHidden` and blank content: the Instructions and Versions tabs show
// this notice instead of an empty editor that reads as "no instructions".
// Only the owner acts on it, so "Run now", "Discuss with Copilot" and the
// trigger editor are not offered either, and the Overview's last action says
// it is private rather than rendering empty.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import type { AgentDetail } from "@/lib/hooks-agent-detail";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { get: vi.fn(() => Promise.resolve([])), post: vi.fn() } };
});

vi.mock("@/lib/governance-hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/governance-hooks")>();
  return { ...actual, useMay: () => () => true, useMayInDepartment: () => () => true };
});

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useAgentKpis: () => ({ data: undefined }),
    useTenantKpis: () => ({ data: { rows: [] } }),
  };
});

import {
  AgentStartActions,
  AgentTriggerPanel,
  OverviewTab,
  PrivateContentNotice,
} from "@/routes/agents.$id";

const AGENT: AgentDetail = {
  id: "agent-1",
  name: "Pia",
  role: "Mail assistant",
  llm: "claude-sonnet-5",
  provider: "anthropic",
  status: "idle",
  tools: [],
  lastAction: "Drafted a reply to the landlord",
  lastRun: "2 min ago",
  tasksToday: 0,
  guardrails: [],
  schedule: "",
  avatarColor: "#000",
  departmentId: "dept-1",
  modelConfigId: null,
  isLead: false,
  mission: "",
  departmentName: "Owner",
  effectiveTools: {},
  departmentFrameTools: {},
  narrowingTools: {},
  runtimeRef: null,
  currentRunId: null,
};

const HIDDEN: AgentDetail = { ...AGENT, lastAction: "", lastRun: "", contentHidden: true };

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("private content notice", () => {
  it("says the content belongs to the owner, without an editor", () => {
    render(<PrivateContentNotice />);
    expect(
      screen.getByText(/only that member can see its runs, files, memory and activity/i),
    ).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).toBeNull();
  });
});

describe("only the owner acts on a personal agent", () => {
  it("offers neither Run now nor Discuss with Copilot when the content is hidden", () => {
    wrap(<AgentStartActions agent={HIDDEN} running={false} busy={false} onRunNow={() => {}} />);
    expect(screen.queryByRole("button", { name: /run now/i })).toBeNull();
    expect(screen.queryByText(/discuss with copilot/i)).toBeNull();
  });

  it("still offers both to the owner", () => {
    wrap(<AgentStartActions agent={AGENT} running={false} busy={false} onRunNow={() => {}} />);
    expect(screen.getByRole("button", { name: /run now/i })).toBeInTheDocument();
    expect(screen.getByText(/discuss with copilot/i)).toBeInTheDocument();
  });

  it("shows the notice instead of the trigger editor", () => {
    wrap(<AgentTriggerPanel agent={HIDDEN} />);
    expect(
      screen.getByText(/only that member can see its runs, files, memory and activity/i),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /cron/i })).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("says the last action is private instead of leaving it empty", () => {
    wrap(<OverviewTab agent={HIDDEN} />);
    expect(screen.getByText("Last action")).toBeInTheDocument();
    expect(
      screen.getByText(/only that member can see its runs, files, memory and activity/i),
    ).toBeInTheDocument();
  });

  it("keeps the owner's last action", () => {
    wrap(<OverviewTab agent={AGENT} />);
    expect(screen.getByText("Drafted a reply to the landlord")).toBeInTheDocument();
    expect(
      screen.queryByText(/only that member can see its runs, files, memory and activity/i),
    ).toBeNull();
  });
});
