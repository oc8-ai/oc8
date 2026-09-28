import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi, beforeEach } from "vitest";
import type { ModelDTO, ModelProviderDTO, ModelWriteBody } from "@/lib/hooks";

const modelsMock = vi.fn();
const providersMock = vi.fn();
const credentialsMock = vi.fn();
const modelPricesMock = vi.fn();
const createModelMutateAsync = vi.fn();
const updateModelMutateAsync = vi.fn();

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useModels: () => modelsMock(),
    useModelProviders: () => providersMock(),
    useCredentials: () => credentialsMock(),
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
  { canonical: "auto", locality: "cloud", available: true, label: "Auto (complexity router)" },
];

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ModelsPage />
    </QueryClientProvider>,
  );
}

function openNewModelDialog() {
  fireEvent.click(screen.getByRole("button", { name: /new model/i }));
  const dialog = screen
    .getByText(/registers a model config|virtual router/i)
    .closest(".max-w-lg") as HTMLElement | null;
  if (!dialog) throw new Error("dialog not found");
  return dialog;
}

function selectAutoProvider(dialog: HTMLElement) {
  const providerSelect = within(dialog).getAllByRole("combobox")[0];
  fireEvent.change(providerSelect, { target: { value: "auto" } });
  return screen.getByText(/virtual router/i).closest(".max-w-lg") as HTMLElement;
}

describe("Model form dialog Auto provider", () => {
  beforeEach(() => {
    credentialsMock.mockReset().mockReturnValue({ data: [] });
    providersMock.mockReset().mockReturnValue({ data: PROVIDERS });
    modelPricesMock.mockReset().mockReturnValue({ data: [] });
    createModelMutateAsync.mockReset().mockResolvedValue({});
    updateModelMutateAsync.mockReset().mockResolvedValue({});
    modelsMock.mockReturnValue({
      data: [
        model({ id: "m-fast", model: "haiku", displayName: "Haiku", name: "Haiku" }),
        model({
          id: "m-strong",
          provider: "openai",
          model: "gpt-4o",
          displayName: "GPT-4o",
          name: "GPT-4o",
        }),
      ],
    });
  });

  it("shows tier pickers and toggles when Auto is selected, not discover/tag", () => {
    renderPage();
    const dialog = selectAutoProvider(openNewModelDialog());

    expect(within(dialog).queryByRole("button", { name: /fetch available models/i })).toBeNull();
    expect(within(dialog).queryByPlaceholderText(/llama3\.1/i)).toBeNull();
    expect(within(dialog).getByText(/tier models/i)).toBeTruthy();
    expect(within(dialog).getByText(/cascade verify/i)).toBeTruthy();
    expect(within(dialog).getByText(/preference router/i)).toBeTruthy();
    expect(within(dialog).getByText(/shadow only/i)).toBeTruthy();
  });

  it("posts autoTiers and toggles on create", async () => {
    renderPage();
    const dialog = selectAutoProvider(openNewModelDialog());

    const selects = within(dialog).getAllByRole("combobox");
    // provider + fast + balanced + strong
    fireEvent.change(selects[1], { target: { value: "m-fast" } });
    fireEvent.change(selects[3], { target: { value: "m-strong" } });
    const cascadeInput = within(dialog)
      .getByText(/cascade verify/i)
      .closest("label")!
      .querySelector("input")!;
    fireEvent.click(cascadeInput);
    fireEvent.click(within(dialog).getByRole("button", { name: /add auto router/i }));

    await waitFor(() => expect(createModelMutateAsync).toHaveBeenCalled());
    const body = createModelMutateAsync.mock.calls[0][0] as ModelWriteBody;
    expect(body.provider).toBe("auto");
    expect(body.model).toBe("router");
    expect(body.autoTiers).toEqual({ fast: "m-fast", strong: "m-strong" });
    expect(body.autoCascadeVerify).toBe(true);
    expect(body.autoShadowOnly).toBe(false);
  });

  it("shows an Auto router badge on auto rows in the models table", () => {
    modelsMock.mockReturnValue({
      data: [
        model({
          id: "m-auto",
          provider: "auto",
          model: "router",
          name: "Auto",
          displayName: "Auto",
          autoTiers: { balanced: "m-fast" },
        }),
      ],
    });
    renderPage();
    expect(screen.getAllByText(/auto router/i).length).toBeGreaterThan(0);
  });
});
