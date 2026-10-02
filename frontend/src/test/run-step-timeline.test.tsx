import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("@/lib/api", () => ({ api: { get: vi.fn() } }));
import { api } from "@/lib/api";
import { RunStepTimeline, type TimelineRun } from "@/components/run-step-timeline";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const catalogue = {
  connection: "odoo",
  labels: [
    {
      tool: "search_records",
      verb: "Looked up",
      object: "{model_label}",
      running: "Looking up {model_label}",
      verbTranslations: {},
      objectTranslations: {},
      runningTranslations: {},
    },
  ],
  modelLabels: [{ key: "crm.lead", label: "deals", labelTranslations: {} }],
  read: ["search_records"],
  modify: ["create_record"],
};

function run(over: Partial<TimelineRun> = {}): TimelineRun {
  return {
    id: "r1",
    agentId: "a1",
    state: "done",
    steps: 2,
    toolCalls: [
      {
        tool: "search_records",
        arguments: { model: "crm.lead" },
        result: "3 rows",
        step: 1,
        connection: "odoo",
        state: "done",
        durationMs: 1200,
      },
      {
        tool: "create_record",
        arguments: { model: "crm.lead", values: { name: "Acme" } },
        result: "ERROR: refused",
        step: 2,
        connection: "odoo",
        state: "denied",
        reason: "above the department limit",
      },
    ],
    stepTimings: [
      { step: 1, stepWallMs: 1200 },
      { step: 2, stepWallMs: 800 },
    ],
    ...over,
  };
}

describe("RunStepTimeline", () => {
  it("shows a summary for a finished run instead of disappearing", async () => {
    vi.mocked(api.get).mockResolvedValue(catalogue);
    render(<RunStepTimeline run={run()} />, { wrapper });
    expect(await screen.findByText(/2 steps/)).toBeInTheDocument();
    expect(screen.getByText(/2\.0s|2s/)).toBeInTheDocument();
  });

  it("renders one labelled row per step when expanded", async () => {
    vi.mocked(api.get).mockResolvedValue(catalogue);
    render(<RunStepTimeline run={run()} defaultOpen />, { wrapper });
    expect(await screen.findByText("Looked up deals")).toBeInTheDocument();
    // No declared label for create_record -> the right-derived fallback.
    expect(screen.getByText("Changed something in odoo")).toBeInTheDocument();
  });

  it("never shows a raw tool name when a right is known", async () => {
    vi.mocked(api.get).mockResolvedValue(catalogue);
    render(<RunStepTimeline run={run()} defaultOpen />, { wrapper });
    await screen.findByText("Looked up deals");
    expect(screen.queryByText(/search_records/)).not.toBeInTheDocument();
    expect(screen.queryByText(/create_record/)).not.toBeInTheDocument();
  });

  it("shows a row's arguments, result and denial reason on expand", async () => {
    vi.mocked(api.get).mockResolvedValue(catalogue);
    render(<RunStepTimeline run={run()} defaultOpen />, { wrapper });
    fireEvent.click(await screen.findByText("Changed something in odoo"));
    expect(screen.getByText(/above the department limit/)).toBeInTheDocument();
    expect(screen.getByText(/ERROR: refused/)).toBeInTheDocument();
    expect(screen.getByText("values")).toBeInTheDocument();
  });

  it("says so explicitly when a run has steps but no detail", async () => {
    vi.mocked(api.get).mockResolvedValue(catalogue);
    render(<RunStepTimeline run={run({ toolCalls: [], steps: 15 })} defaultOpen />, { wrapper });
    expect(await screen.findByText(/not available/i)).toBeInTheDocument();
  });

  it("shows a working row while the run is still going", async () => {
    vi.mocked(api.get).mockResolvedValue(catalogue);
    render(<RunStepTimeline run={run({ state: "running" })} defaultOpen />, { wrapper });
    expect(await screen.findByText(/Working…/)).toBeInTheDocument();
  });

  it("groups a long run and expands a group on demand", async () => {
    vi.mocked(api.get).mockResolvedValue(catalogue);
    const toolCalls = Array.from({ length: 9 }, (_, i) => ({
      tool: "search_records",
      arguments: { model: "crm.lead" },
      result: "ok",
      step: i + 1,
      connection: "odoo",
      state: "done",
    }));
    render(<RunStepTimeline run={run({ toolCalls, steps: 9 })} defaultOpen />, { wrapper });
    const header = await screen.findByText(/odoo · 9 actions/);
    expect(screen.queryByText("Looked up deals")).not.toBeInTheDocument();
    fireEvent.click(header);
    expect(await screen.findAllByText("Looked up deals")).toHaveLength(9);
  });

  it("renders without a label catalogue at all", async () => {
    vi.mocked(api.get).mockRejectedValue(new Error("no such connection"));
    render(<RunStepTimeline run={run()} defaultOpen />, { wrapper });
    // Falls all the way to the raw name rather than throwing or blanking.
    expect(await screen.findByText(/search_records/)).toBeInTheDocument();
  });
});
