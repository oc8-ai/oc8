import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { ModelPicker } from "@/components/model-picker";
import type { ModelDTO, ModelProviderDTO } from "@/lib/hooks";

const models: ModelDTO[] = [
  {
    id: "m1",
    provider: "anthropic",
    name: "Claude",
    status: "healthy",
    costTier: "$$",
    latency: "fast",
    assignedTo: [],
    note: "",
    model: "claude-3-5-sonnet",
    locality: "cloud",
    displayName: null,
    usedByCopilot: true,
    credentialId: null,
    healthError: null,
    healthCheckedAt: null,
  },
  {
    id: "m-auto",
    provider: "auto",
    name: "Auto",
    status: "healthy",
    costTier: "$$",
    latency: "—",
    assignedTo: [],
    note: "",
    model: "router",
    locality: "cloud",
    displayName: "Auto",
    usedByCopilot: false,
    credentialId: null,
    healthError: null,
    healthCheckedAt: null,
    autoTiers: { balanced: "m1" },
  },
];
const providers: ModelProviderDTO[] = [
  { canonical: "anthropic", locality: "cloud", available: true },
  { canonical: "auto", locality: "cloud", available: true, label: "Auto (complexity router)" },
];

function renderWithClient(ui: React.ReactElement) {
  const qc = new QueryClient();
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("ModelPicker", () => {
  it("shows an existing model as selectable and reports selection", () => {
    const onSelect = vi.fn();
    renderWithClient(
      <ModelPicker models={models} providers={providers} selectedId="" onSelect={onSelect} />,
    );
    fireEvent.click(screen.getByRole("radio", { name: /Claude/ }));
    expect(onSelect).toHaveBeenCalledWith("m1");
  });

  it("labels Auto as a complexity router", () => {
    renderWithClient(
      <ModelPicker models={models} providers={providers} selectedId="" onSelect={vi.fn()} />,
    );
    expect(screen.getAllByText(/auto router/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/complexity router/i)).toBeInTheDocument();
  });

  it("shows the connect-a-model form when there are no models yet", () => {
    renderWithClient(
      <ModelPicker models={[]} providers={providers} selectedId="" onSelect={vi.fn()} />,
    );
    expect(screen.getByText(/no model connected yet/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/model tag/i)).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /auto/i })).toBeNull();
  });
});
