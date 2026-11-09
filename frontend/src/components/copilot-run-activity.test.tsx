import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

// The notice links through the router (no full page reload). Stub its Link
// as an anchor that renders the href it would navigate to.
vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return {
    ...actual,
    Link: ({
      children,
      to,
      params,
      search,
    }: {
      children?: ReactNode;
      to: string;
      params?: Record<string, string>;
      search?: Record<string, string>;
    }) => {
      const path = to.replace(/\$(\w+)/g, (_, key: string) => params?.[key] ?? "");
      const query = search ? `?${new URLSearchParams(search).toString()}` : "";
      return (
        <a href={`${path}${query}`} data-router-link="">
          {children}
        </a>
      );
    },
  };
});
vi.mock("@/lib/api", () => ({ api: { get: vi.fn() } }));
import { api } from "@/lib/api";
import { CopilotRunActivity } from "@/components/copilot-run-activity";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("CopilotRunActivity", () => {
  it("renders nothing without a runId", () => {
    const { container } = render(<CopilotRunActivity sessionId="s1" runId={null} />, { wrapper });
    expect(container).toBeEmptyDOMElement();
  });

  it("shows a working indicator with the latest tool call while running", async () => {
    vi.mocked(api.get).mockResolvedValue({
      id: "r1",
      agentId: "a1",
      state: "running",
      phase: null,
      output: null,
      steps: 3,
      toolCalls: [
        {
          tool: "department_status",
          arguments: {},
          result: "ok",
          step: 1,
          connection: null,
          state: "running",
        },
      ],
      taskId: null,
      question: null,
      renderedComponents: [],
    });
    render(<CopilotRunActivity sessionId="s1" runId="r1" />, { wrapper });
    // `department_status` is a core control tool, so it reads as a sentence
    // with no catalogue involved at all. (Narrowed from the brief's
    // `/Checked the department|Working/`: the timeline is open by default
    // while a run is live, so "Working" also matches the summary row and
    // the trailing live indicator, making the broader regex ambiguous --
    // see the task report.)
    expect(await screen.findByText(/Checked the department/)).toBeInTheDocument();
  });

  it("shows an explicit failed-run notice", async () => {
    vi.mocked(api.get).mockResolvedValue({
      id: "r1",
      agentId: "a1",
      state: "failed",
      phase: null,
      output: null,
      steps: 1,
      toolCalls: [],
      taskId: null,
      question: null,
      renderedComponents: [],
    });
    render(<CopilotRunActivity sessionId="s1" runId="r1" />, { wrapper });
    expect(await screen.findByText(/failed/i)).toBeInTheDocument();
  });

  it("shows the run's failure output under the failed-run notice when present", async () => {
    vi.mocked(api.get).mockResolvedValue({
      id: "r1",
      agentId: "a1",
      state: "failed",
      phase: null,
      output: "the department lookup timed out",
      steps: 1,
      toolCalls: [],
      taskId: null,
      question: null,
      renderedComponents: [],
    });
    render(<CopilotRunActivity sessionId="s1" runId="r1" />, { wrapper });
    expect(await screen.findByText(/the department lookup timed out/)).toBeInTheDocument();
  });

  it("shows the run's phase alongside the step count while working", async () => {
    vi.mocked(api.get).mockResolvedValue({
      id: "r1",
      agentId: "a1",
      state: "running",
      phase: "calling tools",
      output: null,
      steps: 3,
      toolCalls: [
        {
          tool: "department_status",
          arguments: {},
          result: "ok",
          step: 1,
          connection: null,
          state: "running",
        },
      ],
      taskId: null,
      question: null,
      renderedComponents: [],
    });
    render(<CopilotRunActivity sessionId="s1" runId="r1" />, { wrapper });
    expect(await screen.findByText(/calling tools/)).toBeInTheDocument();
    expect(await screen.findByText(/3/)).toBeInTheDocument();
  });

  it("keeps a finished run's record instead of disappearing", async () => {
    vi.mocked(api.get).mockResolvedValue({
      id: "r1",
      agentId: "a1",
      state: "done",
      phase: null,
      output: "x",
      steps: 2,
      toolCalls: [
        {
          tool: "department_status",
          arguments: {},
          result: "ok",
          step: 1,
          connection: null,
          state: "done",
        },
      ],
      stepTimings: [{ step: 1, stepWallMs: 1200 }],
      taskId: null,
      question: null,
      renderedComponents: [],
    });
    render(<CopilotRunActivity sessionId="s1" runId="r1" />, { wrapper });
    // The record stays where the work happened -- reviewing what an agent
    // did yesterday is the core interaction this surface exists for.
    expect(await screen.findByText(/2 steps/)).toBeInTheDocument();
  });
  it("offers to connect the account a tool call needed", async () => {
    vi.mocked(api.get).mockResolvedValue({
      id: "r1",
      agentId: "a1",
      state: "succeeded",
      phase: null,
      output: "done",
      steps: 1,
      toolCalls: [],
      taskId: null,
      question: null,
      renderedComponents: [],
      connectionRequired: {
        integration: "microsoft365",
        provider: "microsoft",
        label: "Microsoft 365",
        reason: "not_connected",
      },
    });
    render(<CopilotRunActivity sessionId="s1" runId="r1" />, { wrapper });
    expect(
      await screen.findByText("For this task the agent needs your Microsoft account."),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Connect now →" })).toHaveAttribute(
      "href",
      "/connections?connect=microsoft365",
    );
  });
});
