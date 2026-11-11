import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ModelDTO } from "@/lib/hooks";
import type { AgentDetail } from "@/lib/hooks-agent-detail";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const switchModelMutate = vi.fn();

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useSwitchAgentModel: () => ({ mutate: switchModelMutate, isPending: false }),
  };
});

import { AssignedModelPanel } from "@/routes/agents.$id";

const AGENT: AgentDetail = {
  id: "agent-1",
  name: "Nora",
  role: "Sales",
  llm: "claude-3-5-sonnet",
  provider: "anthropic",
  status: "running",
  tools: [],
  lastAction: "",
  lastRun: "",
  tasksToday: 0,
  guardrails: [],
  schedule: "",
  avatarColor: "#000",
  departmentId: "dept-1",
  modelConfigId: "m-anthropic",
  isLead: false,
  mission: "",
  departmentName: "Vertrieb",
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

function model(overrides: Partial<ModelDTO> = {}): ModelDTO {
  return {
    id: "m-anthropic",
    provider: "anthropic",
    name: "Claude",
    status: "healthy",
    costTier: "$",
    latency: "-",
    assignedTo: [],
    note: "",
    model: "claude-3-5-sonnet",
    locality: "cloud",
    displayName: null,
    usedByCopilot: false,
    credentialId: null,
    healthError: null,
    healthCheckedAt: null,
    supportsReasoning: true,
    ...overrides,
  };
}

const MODELS: ModelDTO[] = [
  model(),
  model({ id: "m-ollama", provider: "ollama", model: "llama3.1:8b", supportsReasoning: false }),
];

function renderPanel(agent: AgentDetail = AGENT, mayManage = true) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AssignedModelPanel agent={agent} models={MODELS} mayManage={mayManage} />
    </QueryClientProvider>,
  );
}

describe("AssignedModelPanel sampling overrides", () => {
  it("shows Effort when the assigned model can think", () => {
    renderPanel();
    expect(screen.getByText("Effort")).toBeInTheDocument();
  });

  it("hides Effort when the assigned model cannot think", () => {
    renderPanel({ ...AGENT, modelConfigId: "m-ollama" });
    expect(screen.queryByText("Effort")).toBeNull();
  });

  it("shows Temperature and Max tokens regardless of provider", () => {
    renderPanel({ ...AGENT, modelConfigId: "m-ollama" });
    expect(screen.getByText("Temperature")).toBeInTheDocument();
    expect(screen.getByText("Max tokens")).toBeInTheDocument();
  });

  it("pre-fills fields from the agent's existing overrides", () => {
    renderPanel({ ...AGENT, temperature: 0.9, maxTokens: 4096, effort: "high" });
    expect(screen.getByDisplayValue("0.9")).toBeInTheDocument();
    expect(screen.getByDisplayValue("4096")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Effort" })).toHaveValue("high");
  });

  it("saves a level picked from the select", () => {
    switchModelMutate.mockReset();
    renderPanel();
    fireEvent.change(screen.getByRole("combobox", { name: "Effort" }), {
      target: { value: "low" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save sampling settings/i }));
    expect(switchModelMutate).toHaveBeenCalledWith(
      expect.objectContaining({ effort: "low" }),
      expect.anything(),
    );
  });

  it("saving sends parsed values plus the unchanged modelConfigId", () => {
    switchModelMutate.mockReset();
    renderPanel();
    // Temperature is the first of the "Inherited"-placeholder fields.
    fireEvent.change(screen.getAllByPlaceholderText("Inherited")[0], {
      target: { value: "0.7" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save sampling settings/i }));

    expect(switchModelMutate).toHaveBeenCalledWith(
      expect.objectContaining({
        agentId: "agent-1",
        modelConfigId: "m-anthropic",
        temperature: 0.7,
        maxTokens: null,
        effort: null,
      }),
      expect.anything(),
    );
  });

  it("never sends an effort override for a model whose provider hides the field", () => {
    switchModelMutate.mockReset();
    renderPanel({ ...AGENT, modelConfigId: "m-ollama" });
    fireEvent.click(screen.getByRole("button", { name: /save sampling settings/i }));

    expect(switchModelMutate).toHaveBeenCalledWith(
      expect.objectContaining({ effort: undefined }),
      expect.anything(),
    );
  });

  it("disables the fields and hides Save when the caller may not manage the agent", () => {
    renderPanel(AGENT, false);
    expect(screen.getByText("Temperature").closest("label")?.querySelector("input")).toBeDisabled();
    expect(screen.queryByRole("button", { name: /save sampling settings/i })).toBeNull();
  });
});

describe("AssignedModelPanel advanced/raw parameters", () => {
  it("shows the raw-params editor for an anthropic assigned model", () => {
    renderPanel();
    expect(screen.getByText(/Advanced\/Raw parameters/i)).toBeInTheDocument();
  });

  it("hides the raw-params editor for a provider its adapter never merges extra for", () => {
    renderPanel({ ...AGENT, modelConfigId: "m-ollama" });
    expect(screen.queryByText(/Advanced\/Raw parameters/i)).toBeNull();
  });

  it("pre-fills rows from the agent's existing extra, JSON-encoding non-string values", () => {
    renderPanel({ ...AGENT, extra: { top_p: 0.9, note: "keep it terse" } });
    expect(screen.getByDisplayValue("top_p")).toBeInTheDocument();
    expect(screen.getByDisplayValue("0.9")).toBeInTheDocument();
    expect(screen.getByDisplayValue("note")).toBeInTheDocument();
    expect(screen.getByDisplayValue("keep it terse")).toBeInTheDocument();
  });

  it("saving parses a JSON-valued row and merges it into the switch-model payload", () => {
    switchModelMutate.mockReset();
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: /add parameter/i }));
    fireEvent.change(screen.getByPlaceholderText("key"), { target: { value: "top_p" } });
    fireEvent.change(screen.getByPlaceholderText("value"), { target: { value: "0.9" } });
    fireEvent.click(screen.getByRole("button", { name: /save sampling settings/i }));

    expect(switchModelMutate).toHaveBeenCalledWith(
      expect.objectContaining({ extra: { top_p: 0.9 } }),
      expect.anything(),
    );
  });

  it("saving with every row cleared sends null, clearing back to inherit", () => {
    switchModelMutate.mockReset();
    renderPanel({ ...AGENT, extra: { top_p: 0.9 } });
    fireEvent.click(screen.getByRole("button", { name: /remove parameter/i }));
    fireEvent.click(screen.getByRole("button", { name: /save sampling settings/i }));

    expect(switchModelMutate).toHaveBeenCalledWith(
      expect.objectContaining({ extra: null }),
      expect.anything(),
    );
  });

  it("never sends an extra override for a model whose provider hides the editor", () => {
    switchModelMutate.mockReset();
    renderPanel({ ...AGENT, modelConfigId: "m-ollama" });
    fireEvent.click(screen.getByRole("button", { name: /save sampling settings/i }));

    expect(switchModelMutate).toHaveBeenCalledWith(
      expect.objectContaining({ extra: undefined }),
      expect.anything(),
    );
  });
});
