import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi, beforeEach } from "vitest";
import type { ModelDTO, ModelProviderDTO, ModelWriteBody } from "@/lib/hooks";

const modelsMock = vi.fn();
const providersMock = vi.fn();
const credentialsMock = vi.fn();
const modelPricesMock = vi.fn();
const updateModelMutateAsync = vi.fn();

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useModels: () => modelsMock(),
    useModelProviders: () => providersMock(),
    useCredentials: () => credentialsMock(),
    useUpdateModel: () => ({ mutateAsync: updateModelMutateAsync, isPending: false }),
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
    useModelPrices: () => modelPricesMock(),
    useCreateModelPrice: () => ({ mutateAsync: vi.fn(), isPending: false }),
    useDeactivateModelPrice: () => ({ mutateAsync: vi.fn(), isPending: false }),
  };
});

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { ModelsPage } from "@/routes/models";

function model(overrides: Partial<ModelDTO> = {}): ModelDTO {
  return {
    id: "m1",
    provider: "openrouter",
    name: "Lennart's model",
    status: "healthy",
    costTier: "$",
    latency: "-",
    assignedTo: [],
    note: "",
    model: "z-ai/glm-5.3-flash",
    locality: "cloud",
    displayName: null,
    usedByCopilot: false,
    credentialId: null,
    healthError: null,
    healthCheckedAt: null,
    ...overrides,
  };
}

const PROVIDERS: ModelProviderDTO[] = [
  { canonical: "openrouter", locality: "cloud", available: true },
];

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ModelsPage />
    </QueryClientProvider>,
  );
}

describe("Model edit dialog's max output tokens field", () => {
  beforeEach(() => {
    credentialsMock.mockReset().mockReturnValue({ data: [] });
    providersMock.mockReset().mockReturnValue({ data: PROVIDERS });
    modelPricesMock.mockReset().mockReturnValue({ data: [] });
    updateModelMutateAsync.mockReset().mockResolvedValue({});
  });

  it("is absent from the create-model dialog", () => {
    modelsMock.mockReturnValue({ data: [] });
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: /new model/i }));
    expect(screen.queryByText(/max output tokens/i)).toBeNull();
  });

  it("prefills from the model's existing maxTokens on edit", () => {
    modelsMock.mockReturnValue({ data: [model({ id: "m1", maxTokens: 8192 })] });
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: /edit/i }));
    const field = screen.getByPlaceholderText("4096") as HTMLInputElement;
    expect(field.value).toBe("8192");
  });

  it("sends the typed value as maxTokens on save", async () => {
    modelsMock.mockReturnValue({ data: [model({ id: "m1", maxTokens: null })] });
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: /edit/i }));
    const field = screen.getByPlaceholderText("4096") as HTMLInputElement;
    fireEvent.change(field, { target: { value: "8192" } });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => expect(updateModelMutateAsync).toHaveBeenCalled());
    const call = updateModelMutateAsync.mock.calls[0][0] as ModelWriteBody & { id: string };
    expect(call.id).toBe("m1");
    expect(call.maxTokens).toBe(8192);
  });

  it("sends 0 (clear) when the field is left blank", async () => {
    modelsMock.mockReturnValue({ data: [model({ id: "m1", maxTokens: 8192 })] });
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: /edit/i }));
    const field = screen.getByPlaceholderText("4096") as HTMLInputElement;
    fireEvent.change(field, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => expect(updateModelMutateAsync).toHaveBeenCalled());
    const call = updateModelMutateAsync.mock.calls[0][0] as ModelWriteBody & { id: string };
    expect(call.maxTokens).toBe(0);
  });
});
