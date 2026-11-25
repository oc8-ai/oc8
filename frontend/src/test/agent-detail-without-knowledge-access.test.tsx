// frontend/src/test/agent-detail-without-knowledge-access.test.tsx
// A member who owns a personal agent may not list the knowledge bases
// (`GET /knowledge/bases` answers 403). The agent page must still settle:
// with no bases it once seeded the agent's knowledge picker from a fresh
// `[]` on every render, which re-ran the seeding effect for ever and crashed
// the Live tab (its Radix switch turned the loop into "Maximum update depth
// exceeded").
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async (orig) => {
  const actual = await orig<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: { get, post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  };
});
vi.mock("@tanstack/react-router", async (orig) => ({
  ...(await orig<typeof import("@tanstack/react-router")>()),
  Link: ({ children }: { children: ReactNode }) => <a href="/">{children}</a>,
  useNavigate: () => vi.fn(),
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { ApiError } from "@/lib/api";
import { Route } from "@/routes/agents.$id";

const AGENT_ID = "01a20fd0-6d65-728e-b216-1a0b220ce5b0";

const AGENT = {
  id: AGENT_ID,
  name: "Mailbot",
  role: "Personal mail assistant",
  llm: "",
  provider: "",
  status: "idle",
  tools: [],
  lastAction: "",
  lastRun: "",
  tasksToday: 0,
  guardrails: [],
  schedule: "",
  avatarColor: "#000",
  departmentId: "dept-personal",
  modelConfigId: null,
  isLead: false,
  mission: "",
  departmentName: "dev-member",
  effectiveTools: {},
  departmentFrameTools: {},
  narrowingTools: {},
  runtimeRef: null,
  currentRunId: null,
  temperature: null,
  maxTokens: null,
  effort: null,
  extra: null,
};

// A plain member who owns the agent's personal department.
const GOVERNANCE = {
  callerRole: "member",
  callerPermissions: ["connection:own", "copilot:use", "department:own"],
  seats: [
    {
      departmentId: "dept-personal",
      departmentName: "dev-member",
      seatRole: "dept_approver",
      agentManage: true,
      ownerPermissions: ["agent_version:publish", "agent_version:view", "run:start", "run:view"],
    },
  ],
};

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

let errors: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  get.mockReset();
  get.mockImplementation((path: string) => {
    if (path === `/agents/${AGENT_ID}`) return Promise.resolve(AGENT);
    if (path === "/governance") return Promise.resolve(GOVERNANCE);
    if (path.startsWith("/knowledge/bases")) {
      return Promise.reject(new ApiError("forbidden", 403));
    }
    return Promise.resolve([]);
  });
  errors = vi.spyOn(console, "error").mockImplementation(() => undefined);
});

afterEach(() => {
  errors.mockRestore();
});

describe("Agent page for a reader who may not list knowledge bases", () => {
  it("settles instead of re-seeding the knowledge picker on every render", async () => {
    vi.spyOn(Route, "useParams").mockReturnValue({ id: AGENT_ID } as never);
    vi.spyOn(Route, "useSearch").mockReturnValue({ tab: "overview" } as never);
    const Page = Route.options.component as () => ReactNode;
    render(<Page />, { wrapper });
    expect((await screen.findAllByText("Mailbot")).length).toBeGreaterThan(0);
    // Let the 403 land and any effect loop run.
    await new Promise((resolve) => setTimeout(resolve, 200));
    const loops = errors.mock.calls.filter((call: unknown[]) =>
      String(call[0]).includes("Maximum update depth"),
    );
    expect(loops).toEqual([]);
  });
});
