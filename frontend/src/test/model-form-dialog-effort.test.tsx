import { render, screen, within, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi, beforeEach } from "vitest";
import type { ModelDTO, ModelProviderDTO, ModelWriteBody } from "@/lib/hooks";

const modelsMock = vi.fn();
const providersMock = vi.fn();
const credentialsMock = vi.fn();
const modelPricesMock = vi.fn();
const reasoningSupportMock = vi.fn();
const createModelMutateAsync = vi.fn();
const updateModelMutateAsync = vi.fn();

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useModels: () => modelsMock(),
    useModelProviders: () => providersMock(),
    useCredentials: () => credentialsMock(),
    useReasoningSupport: (provider: string, model: string) => reasoningSupportMock(provider, model),
    useCreateModel: () => ({ mutateAsync: createModelMutateAsync, isPending: false }),
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
    provider: "anthropic",
    name: "Nora's model",
    status: "healthy",
    costTier: "$",
    latency: "-",
    assignedTo: [],
    note: "",
    model: "claude-sonnet-5",
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
  { canonical: "anthropic", locality: "cloud", available: true },
];

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ModelsPage />
    </QueryClientProvider>,
  );
}

function openCreate() {
  modelsMock.mockReturnValue({ data: [] });
  renderPage();
  fireEvent.click(screen.getByRole("button", { name: /new model/i }));
  fireEvent.change(screen.getByPlaceholderText(/e\.g\. sales gpt/i), {
    target: { value: "Claude" },
  });
  fireEvent.change(screen.getByPlaceholderText(/llama3\.1:8b/i), {
    target: { value: "claude-sonnet-5" },
  });
}

function submitCreate() {
  const dialog = screen
    .getByText(/registers a model config/i)
    .closest(".max-w-lg") as HTMLElement | null;
  if (!dialog) throw new Error("dialog not found");
  fireEvent.click(within(dialog).getByRole("button", { name: /^add model$/i }));
}

function effortSelect() {
  return screen.getByRole("combobox", { name: "Effort" }) as HTMLSelectElement;
}

describe("Model form dialog: effort and Can think", () => {
  beforeEach(() => {
    credentialsMock.mockReset().mockReturnValue({ data: [] });
    providersMock.mockReset().mockReturnValue({ data: PROVIDERS });
    modelPricesMock.mockReset().mockReturnValue({ data: [] });
    reasoningSupportMock.mockReset().mockReturnValue({ data: undefined });
    createModelMutateAsync.mockReset().mockResolvedValue({});
    updateModelMutateAsync.mockReset().mockResolvedValue({});
  });

  it("offers the three levels and a custom value", () => {
    openCreate();
    const options = within(effortSelect())
      .getAllByRole("option")
      .map((o) => o.textContent);
    expect(options).toEqual(["Not set", "Fast", "Balanced", "Thorough", "Custom value"]);
  });

  it("writes a level as low/medium/high on create", async () => {
    openCreate();
    fireEvent.change(effortSelect(), { target: { value: "high" } });
    submitCreate();
    await waitFor(() => expect(createModelMutateAsync).toHaveBeenCalled());
    expect((createModelMutateAsync.mock.calls[0][0] as ModelWriteBody).effort).toBe("high");
  });

  it("keeps a custom value verbatim", async () => {
    openCreate();
    fireEvent.change(effortSelect(), { target: { value: "custom" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Custom effort value" }), {
      target: { value: "max" },
    });
    submitCreate();
    await waitFor(() => expect(createModelMutateAsync).toHaveBeenCalled());
    expect((createModelMutateAsync.mock.calls[0][0] as ModelWriteBody).effort).toBe("max");
  });

  it("prefills Can think from the suggestion and leaves the decision to the server", async () => {
    reasoningSupportMock.mockReturnValue({ data: { supportsReasoning: true } });
    openCreate();
    expect(reasoningSupportMock).toHaveBeenLastCalledWith("anthropic", "claude-sonnet-5");
    expect(screen.getByRole("checkbox", { name: /can think/i })).toBeChecked();
    submitCreate();
    await waitFor(() => expect(createModelMutateAsync).toHaveBeenCalled());
    expect(createModelMutateAsync.mock.calls[0][0]).not.toHaveProperty("supportsReasoning");
  });

  it("sends Can think once the admin changes it", async () => {
    reasoningSupportMock.mockReturnValue({ data: { supportsReasoning: true } });
    openCreate();
    fireEvent.click(screen.getByRole("checkbox", { name: /can think/i }));
    submitCreate();
    await waitFor(() => expect(createModelMutateAsync).toHaveBeenCalled());
    expect((createModelMutateAsync.mock.calls[0][0] as ModelWriteBody).supportsReasoning).toBe(
      false,
    );
  });

  it("prefills a level, a custom value and Can think on edit", () => {
    modelsMock.mockReturnValue({
      data: [model({ id: "m1", effort: "medium-high", supportsReasoning: true })],
    });
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /edit/i }));
    expect(effortSelect().value).toBe("custom");
    expect(screen.getByRole("textbox", { name: "Custom effort value" })).toHaveValue("medium-high");
    expect(screen.getByRole("checkbox", { name: /can think/i })).toBeChecked();
  });

  it("saves a level and an emptied field on edit", async () => {
    modelsMock.mockReturnValue({ data: [model({ id: "m1", effort: "MEDIUM" })] });
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /edit/i }));
    expect(effortSelect().value).toBe("medium");
    fireEvent.change(effortSelect(), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    await waitFor(() => expect(updateModelMutateAsync).toHaveBeenCalled());
    const call = updateModelMutateAsync.mock.calls[0][0] as ModelWriteBody & { id: string };
    expect(call.effort).toBe("");
    expect(call.supportsReasoning).toBe(false);
  });
});
