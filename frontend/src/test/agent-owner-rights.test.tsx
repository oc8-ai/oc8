// frontend/src/test/agent-owner-rights.test.tsx
// The owner of a personal department is usually a plain `member`: no
// `run:start` and no `agent_version:publish` in `callerPermissions`. Their seat
// in their own personal department carries both there (`Seat.ownerPermissions`),
// so on their own agent they get Run now and the publish controls -- and on an
// agent in any other department, still nothing. Real governance hooks; only the
// API is stubbed.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentDetail } from "@/lib/hooks-agent-detail";

const { getMock } = vi.hoisted(() => ({ getMock: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { get: (url: string) => getMock(url), post: vi.fn() } };
});

import { AgentPublishBar } from "@/components/agent-publish-bar";
import { AgentVersionsTab } from "@/components/agent-versions-tab";
import { AgentStartActions } from "@/routes/agents.$id";

const MINE = "dept-personal";
const ELSEWHERE = "dept-sales";

const GOVERNANCE = {
  callerRole: "member",
  callerPermissions: ["connection:own", "copilot:use", "department:own"],
  seats: [
    {
      departmentId: MINE,
      departmentName: "Owner",
      seatRole: "dept_approver",
      agentManage: true,
      ownerPermissions: [
        "agent_version:publish",
        "agent_version:view",
        "run:control",
        "run:start",
        "run:view",
      ],
    },
  ],
};

const VERSIONS = {
  items: [
    {
      id: "v2",
      versionNo: 2,
      note: null,
      publishedBy: "mem-1",
      publishedAt: "2026-11-24T12:00:00+00:00",
      isCurrent: true,
      rolledBackFrom: null,
    },
    {
      id: "v1",
      versionNo: 1,
      note: null,
      publishedBy: null,
      publishedAt: "2026-11-23T12:00:00+00:00",
      isCurrent: false,
      rolledBackFrom: null,
    },
  ],
  totalCount: 2,
};

function agentIn(departmentId: string): AgentDetail {
  return {
    id: "agent-1",
    name: "Pia",
    role: "Mail assistant",
    llm: "claude-sonnet-5",
    provider: "anthropic",
    status: "idle",
    tools: [],
    lastAction: "",
    lastRun: "",
    tasksToday: 0,
    guardrails: [],
    schedule: "",
    avatarColor: "#000",
    departmentId,
    modelConfigId: null,
    isLead: false,
    mission: "Answer my mail",
    departmentName: "Owner",
    effectiveTools: {},
    departmentFrameTools: {},
    narrowingTools: {},
    runtimeRef: null,
    currentRunId: null,
  };
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("a member who owns a personal department", () => {
  beforeEach(() => {
    getMock.mockReset();
    getMock.mockImplementation((url: string) => {
      if (url === "/governance") return Promise.resolve(GOVERNANCE);
      if (url.includes("/draft-status")) {
        return Promise.resolve({ dirty: true, changedFields: ["mission"], currentVersionNo: 2 });
      }
      if (url.includes("/versions")) return Promise.resolve(VERSIONS);
      return Promise.resolve([]);
    });
  });

  it("sees Run now on their own personal agent", async () => {
    wrap(
      <AgentStartActions agent={agentIn(MINE)} running={false} busy={false} onRunNow={() => {}} />,
    );
    expect(await screen.findByRole("button", { name: /run now/i })).toBeInTheDocument();
  });

  it("is not offered Run now on an agent of another department", async () => {
    wrap(
      <AgentStartActions
        agent={agentIn(ELSEWHERE)}
        running={false}
        busy={false}
        onRunNow={() => {}}
      />,
    );
    await screen.findByText(/discuss with copilot/i);
    await vi.waitFor(() => expect(getMock).toHaveBeenCalledWith("/governance"));
    expect(screen.queryByRole("button", { name: /run now/i })).toBeNull();
  });

  it("sees Publish on their own personal agent's draft", async () => {
    wrap(<AgentPublishBar agentId="agent-1" departmentId={MINE} mayManage={true} />);
    expect(await screen.findByRole("button", { name: /publish/i })).toBeInTheDocument();
  });

  it("is not offered Publish in another department", async () => {
    wrap(<AgentPublishBar agentId="agent-1" departmentId={ELSEWHERE} mayManage={true} />);
    expect(await screen.findByRole("button", { name: /review/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /publish/i })).toBeNull();
  });

  it("sees Roll back on their own personal agent's versions", async () => {
    wrap(<AgentVersionsTab agentId="agent-1" departmentId={MINE} mayManage={true} />);
    const rows = await screen.findAllByRole("row");
    const v1 = rows.find((row) => within(row).queryByText("v1"));
    expect(v1).toBeDefined();
    expect(
      await within(v1 as HTMLElement).findByRole("button", { name: /roll back/i }),
    ).toBeInTheDocument();
  });

  it("is not offered Roll back in another department", async () => {
    wrap(<AgentVersionsTab agentId="agent-1" departmentId={ELSEWHERE} mayManage={true} />);
    const rows = await screen.findAllByRole("row");
    await vi.waitFor(() => expect(getMock).toHaveBeenCalledWith("/governance"));
    for (const row of rows) {
      expect(within(row).queryByRole("button", { name: /roll back/i })).toBeNull();
    }
  });
});
