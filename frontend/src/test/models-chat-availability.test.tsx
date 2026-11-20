import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ModelDTO } from "@/lib/hooks";

const modelsMock = vi.fn();
const updateMock = vi.fn();

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useModels: () => modelsMock(),
    useModelProviders: () => ({ data: [] }),
    useCredentials: () => ({ data: [] }),
    useUpdateModel: () => ({ mutateAsync: updateMock, isPending: false }),
  };
});

vi.mock("@/lib/governance-hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/governance-hooks")>();
  return { ...actual, useMay: () => () => true };
});

vi.mock("@/lib/model-prices-hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/model-prices-hooks")>();
  return {
    ...actual,
    useModelPrices: () => ({ data: [] }),
    useCreateModelPrice: () => ({ mutateAsync: vi.fn(), isPending: false }),
    useDeactivateModelPrice: () => ({ mutateAsync: vi.fn(), isPending: false }),
  };
});

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { ModelsPage } from "@/routes/models";

function model(overrides: Partial<ModelDTO> = {}): ModelDTO {
  return {
    id: "m1",
    provider: "ollama",
    name: "Llama",
    status: "healthy",
    costTier: "$",
    latency: "-",
    assignedTo: [],
    note: "",
    model: "llama3.1:8b",
    locality: "local",
    displayName: null,
    usedByCopilot: false,
    credentialId: null,
    healthError: null,
    healthCheckedAt: null,
    ...overrides,
  };
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ModelsPage />
    </QueryClientProvider>,
  );
}

function chatSwitch() {
  return screen.getByRole("switch", { name: /Available in chat/ });
}

describe("Models table: available in chat", () => {
  beforeEach(() => {
    updateMock.mockReset();
  });

  it("renders the model's current state", () => {
    modelsMock.mockReturnValue({ data: [model({ chatEnabled: false })] });
    renderPage();
    expect(chatSwitch()).toHaveAttribute("aria-checked", "false");
    // The column's hint is said once, not in every row (E2E I).
    expect(chatSwitch()).toHaveAccessibleDescription(
      "Chat: members can pick a model that is available in chat. Agents keep their own default model.",
    );
  });

  it("says the Chat column's hint once for all rows (E2E I)", () => {
    modelsMock.mockReturnValue({
      data: [model(), model({ id: "m2", model: "qwen2.5:3b" })],
    });
    renderPage();
    expect(screen.getAllByRole("switch", { name: /Available in chat/ })).toHaveLength(2);
    expect(screen.getAllByText(/Agents keep their own default model/)).toHaveLength(1);
  });

  it("treats a model without the flag as available", () => {
    modelsMock.mockReturnValue({ data: [model()] });
    renderPage();
    expect(chatSwitch()).toHaveAttribute("aria-checked", "true");
  });

  it("sends chatEnabled with the model's identity when toggled", async () => {
    modelsMock.mockReturnValue({ data: [model({ chatEnabled: true })] });
    updateMock.mockResolvedValue(model({ chatEnabled: false }));
    renderPage();
    fireEvent.click(chatSwitch());
    expect(chatSwitch()).toHaveAttribute("aria-checked", "false");
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith({
        id: "m1",
        provider: "ollama",
        model: "llama3.1:8b",
        locality: "local",
        chatEnabled: false,
      }),
    );
  });

  it("reverts and shows an inline error when saving fails", async () => {
    modelsMock.mockReturnValue({ data: [model({ chatEnabled: true })] });
    updateMock.mockRejectedValue(new Error("boom"));
    renderPage();
    fireEvent.click(chatSwitch());
    await waitFor(() => expect(chatSwitch()).toHaveAttribute("aria-checked", "true"));
    expect(screen.getByRole("alert")).toHaveTextContent("Could not save. Try again.");
  });

  it("has no switch for the auto router", () => {
    modelsMock.mockReturnValue({ data: [model({ provider: "auto", model: "auto" })] });
    renderPage();
    expect(screen.queryByRole("switch", { name: /Available in chat/ })).toBeNull();
  });
});
