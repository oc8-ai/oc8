// frontend/src/test/agent-private-tabs.test.tsx
// A personal agent read by someone other than its owner (`contentHidden`):
// its runs, files, memory and activity are the owner's alone, and the API
// answers each of them with a 404. The Live, Live log, History, Files and
// Memory tabs show the private notice instead -- without asking the API for
// what it will refuse -- and a 404 is never retried. The owner keeps every
// tab as before.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, renderHook, screen, waitFor } from "@testing-library/react";
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
import { useLiveTimeline } from "@/lib/conversation-api";
import { Route } from "@/routes/agents.$id";

const AGENT_ID = "01a20fd0-6d65-728e-b216-1a0b220ce5b0";
const RUN_ID = "01a20fd0-6d65-728e-b216-1a0b220ce5b1";

const AGENT = {
  id: AGENT_ID,
  name: "Pia",
  role: "Mail assistant",
  llm: "",
  provider: "",
  status: "running",
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
  departmentName: "Owner",
  effectiveTools: {},
  departmentFrameTools: {},
  narrowingTools: {},
  runtimeRef: null,
  currentRunId: RUN_ID,
  temperature: null,
  maxTokens: null,
  effort: null,
  extra: null,
};

const GOVERNANCE = {
  callerRole: "org_admin",
  callerPermissions: ["agent:view", "agent:manage", "department:manage", "run:view"],
  seats: [],
};

const NOTICE = /only that member can see its runs, files, memory and activity/i;

// Each tab whose content is the owner's alone, and the reads it would make.
const PRIVATE_TABS = [
  ["live", [`/agents/${AGENT_ID}/live`]],
  ["livelog", [`/runs/${RUN_ID}`, "/activity"]],
  ["history", [`/agents/${AGENT_ID}/runs`]],
  ["files", [`/agents/${AGENT_ID}/workspace/files`]],
  ["memory", [`/agents/${AGENT_ID}/memory`]],
] as const;

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function serve(agent: typeof AGENT & { contentHidden?: boolean }) {
  get.mockImplementation((path: string) => {
    if (path === `/agents/${AGENT_ID}`) return Promise.resolve(agent);
    if (path === "/governance") return Promise.resolve(GOVERNANCE);
    if (path.startsWith(`/agents/${AGENT_ID}/live`)) {
      return Promise.resolve({ items: [], nextBefore: null });
    }
    if (path.startsWith(`/agents/${AGENT_ID}/runs`)) {
      return Promise.resolve({ items: [], total: 0 });
    }
    if (path.startsWith(`/agents/${AGENT_ID}/workspace/files`)) {
      return Promise.resolve({ files: [] });
    }
    if (path.startsWith(`/agents/${AGENT_ID}/memory`)) return Promise.resolve({ records: [] });
    if (path.startsWith("/runs/")) return Promise.reject(new ApiError("run not found", 404));
    return Promise.resolve([]);
  });
}

function requested(prefix: string): boolean {
  return get.mock.calls.some(([path]) => String(path).startsWith(prefix));
}

async function openTab(tab: string) {
  vi.spyOn(Route, "useParams").mockReturnValue({ id: AGENT_ID } as never);
  vi.spyOn(Route, "useSearch").mockReturnValue({ tab } as never);
  const Page = Route.options.component as () => ReactNode;
  render(<Page />, { wrapper });
  expect((await screen.findAllByText("Pia")).length).toBeGreaterThan(0);
}

let errors: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  get.mockReset();
  errors = vi.spyOn(console, "error").mockImplementation(() => undefined);
});

afterEach(() => {
  errors.mockRestore();
  vi.restoreAllMocks();
});

describe("a personal agent's tabs for a reader who is not its owner", () => {
  it.each(PRIVATE_TABS)(
    "shows the private notice instead of Live, History, Files and Memory when contentHidden (%s)",
    async (tab, paths) => {
      serve({ ...AGENT, contentHidden: true });
      await openTab(tab);
      expect(await screen.findByText(NOTICE)).toBeInTheDocument();
      // Let any query that would fire do so.
      await new Promise((resolve) => setTimeout(resolve, 50));
      for (const path of paths) expect(requested(path), path).toBe(false);
    },
  );

  it("does not request the live timeline when contentHidden", async () => {
    serve({ ...AGENT, contentHidden: true });
    await openTab("live");
    await screen.findByText(NOTICE);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(requested(`/agents/${AGENT_ID}/live`)).toBe(false);
  });

  it("keeps the run state in the header without asking for a run", async () => {
    serve({
      ...AGENT,
      status: "waiting_for_approval",
      contentHidden: true,
      currentRunState: "waiting_for_approval",
    } as typeof AGENT & { contentHidden: boolean });
    await openTab("overview");
    expect(await screen.findByText(/waiting for approval/i)).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(requested(`/agents/${AGENT_ID}/runs`)).toBe(false);
    expect(requested("/runs/")).toBe(false);
  });

  it("owner sees the tabs as before", async () => {
    serve({ ...AGENT, contentHidden: false });
    await openTab("files");
    await waitFor(() => expect(requested(`/agents/${AGENT_ID}/workspace/files`)).toBe(true));
    expect(screen.queryByText(NOTICE)).toBeNull();
  });

  it.each(PRIVATE_TABS.filter(([tab]) => tab !== "livelog"))(
    "owner still reads the %s tab",
    async (tab, paths) => {
      serve({ ...AGENT, contentHidden: false });
      await openTab(tab);
      for (const path of paths) await waitFor(() => expect(requested(path), path).toBe(true));
      expect(screen.queryByText(NOTICE)).toBeNull();
    },
  );
});

describe("useLiveTimeline", () => {
  it("live timeline does not retry a 404", async () => {
    get.mockImplementation(() => Promise.reject(new ApiError("agent not found", 404)));
    // Retries on by default, without delay: only the hook's own `retry`
    // keeps a refusal from being asked again.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: 3, retryDelay: 0 } } });
    const { result } = renderHook(() => useLiveTimeline(AGENT_ID, false), {
      wrapper: ({ children }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>,
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(get).toHaveBeenCalledTimes(1);
  });
});
