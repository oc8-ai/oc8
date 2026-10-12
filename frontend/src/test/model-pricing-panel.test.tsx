import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { ModelPricingPanel } from "@/routes/models";
import type { ModelPrice } from "@/lib/model-prices-hooks";
import type { Governance } from "@/lib/governance-hooks";
import type { ModelDTO } from "@/lib/hooks";

const { getPrices, getGovernance, postPrice, deletePrice } = vi.hoisted(() => ({
  getPrices: vi.fn(),
  getGovernance: vi.fn(),
  postPrice: vi.fn(),
  deletePrice: vi.fn(),
}));

const { getModels, getPolicy } = vi.hoisted(() => ({ getModels: vi.fn(), getPolicy: vi.fn() }));

vi.mock("@/lib/api", () => ({
  api: {
    get: (path: string) => {
      if (path === "/model-prices") return getPrices();
      if (path === "/governance") return getGovernance();
      if (path === "/models") return getModels();
      if (path === "/model-prices/policy") return getPolicy();
      return Promise.reject(new Error(`unexpected GET ${path}`));
    },
    post: (path: string, body: unknown) => {
      if (path === "/model-prices") return postPrice(body);
      return Promise.reject(new Error(`unexpected POST ${path}`));
    },
    delete: (path: string) => {
      if (path.startsWith("/model-prices/")) return deletePrice(path);
      return Promise.reject(new Error(`unexpected DELETE ${path}`));
    },
  },
}));

const GOVERNANCE: Governance = {
  permissions: ["model:manage", "model:view"],
  roles: [],
  callerRole: "org_admin",
  callerPermissions: ["model:manage", "model:view"],
  callerRoleIsKnown: true,
};

const PRICES: ModelPrice[] = [
  {
    id: "price-1",
    provider: "anthropic",
    modelPattern: "claudesonnet45",
    priceInUsdPer1M: 3.0,
    priceOutUsdPer1M: 15.0,
    effectiveFrom: "2026-08-01T00:00:00Z",
    active: true,
  },
];

const REGISTERED_MODELS: ModelDTO[] = [
  {
    id: "model-1",
    provider: "anthropic",
    name: "Claude Sonnet 4.5",
    status: "healthy",
    costTier: "$",
    latency: "-",
    assignedTo: [],
    note: "",
    model: "claude-sonnet-4-5-20250929",
    locality: "cloud",
    displayName: "Claude Sonnet 4.5",
    usedByCopilot: false,
    credentialId: null,
    healthError: null,
    healthCheckedAt: null,
  },
  {
    id: "model-2",
    provider: "opaas_ai",
    name: "opaas_ai:odoo-gpt",
    status: "healthy",
    costTier: "$",
    latency: "-",
    assignedTo: [],
    note: "",
    model: "opaas_ai:odoo-gpt",
    locality: "cloud",
    displayName: null,
    usedByCopilot: false,
    credentialId: "cred-1",
    healthError: null,
    healthCheckedAt: null,
  },
];

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ModelPricingPanel />
    </QueryClientProvider>,
  );
}

describe("ModelPricingPanel", () => {
  beforeEach(() => {
    getPrices.mockReset();
    getGovernance.mockReset();
    postPrice.mockReset();
    deletePrice.mockReset();
    getModels.mockReset();
    getGovernance.mockResolvedValue(GOVERNANCE);
    getModels.mockResolvedValue([]);
    getPolicy.mockReset().mockResolvedValue({ readOnly: false });
  });

  it("offers no editing when the operator manages the prices", async () => {
    getPrices.mockResolvedValue(PRICES);
    getPolicy.mockResolvedValue({ readOnly: true });
    renderPanel();

    expect(await screen.findByText(/managed by your platform operator/i)).toBeInTheDocument();
    expect(screen.getByText("claudesonnet45")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /add model/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /edit/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /remove/i })).toBeNull();
  });

  it("renders rows from useModelPrices(), not from usePricing()", async () => {
    getPrices.mockResolvedValue(PRICES);
    renderPanel();

    expect(await screen.findByText("claudesonnet45")).toBeInTheDocument();
    expect(screen.getByText("anthropic")).toBeInTheDocument();
    expect(screen.getByText("3.00")).toBeInTheDocument();
    expect(screen.getByText("15.00")).toBeInTheDocument();
    // The old mock-pricing IDs (from `defaultPricing` in `@/lib/costs`) must
    // never appear -- that would mean the panel fell back to `usePricing()`.
    expect(screen.queryByText("Claude 3.5 Sonnet")).not.toBeInTheDocument();
  });

  it("editing a price row and saving calls the create-mutation with the new value", async () => {
    getPrices.mockResolvedValue(PRICES);
    postPrice.mockResolvedValue({ ...PRICES[0], id: "price-2", priceInUsdPer1M: 4.5 });
    renderPanel();

    await screen.findByText("claudesonnet45");
    fireEvent.click(screen.getByRole("button", { name: /edit/i }));

    const inputs = screen.getAllByRole("spinbutton");
    fireEvent.change(inputs[0], { target: { value: "4.5" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() =>
      expect(postPrice).toHaveBeenCalledWith({
        provider: "anthropic",
        modelPattern: "claudesonnet45",
        priceInUsdPer1M: 4.5,
        priceOutUsdPer1M: 15.0,
      }),
    );
  });

  it("removing a row calls the deactivate-mutation for that row's id", async () => {
    getPrices.mockResolvedValue(PRICES);
    deletePrice.mockResolvedValue(undefined);
    renderPanel();

    await screen.findByText("claudesonnet45");
    fireEvent.click(screen.getByRole("button", { name: /^remove$/i }));
    // The confirm dialog's own action button shares the "Remove" label with
    // the row's trigger button, so two matches exist once it's open.
    const removeButtons = screen.getAllByRole("button", { name: /^remove$/i });
    fireEvent.click(removeButtons[removeButtons.length - 1]);

    await waitFor(() => expect(deletePrice).toHaveBeenCalledWith("/model-prices/price-1"));
  });

  it("offers a dropdown of registered models, not a free-text field, when adding a price", async () => {
    getPrices.mockResolvedValue([]);
    getModels.mockResolvedValue(REGISTERED_MODELS);
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /add model/i }));

    const select = await screen.findByRole("combobox");
    expect(select).toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/model pattern/i)).not.toBeInTheDocument();
    expect(screen.getByRole("option", { name: /claude-sonnet-4-5-20250929/i })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /opaas_ai:odoo-gpt/i })).toBeInTheDocument();
  });

  it("selecting a model from the dropdown sets the provider automatically", async () => {
    getPrices.mockResolvedValue([]);
    getModels.mockResolvedValue(REGISTERED_MODELS);
    postPrice.mockResolvedValue({});
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /add model/i }));
    const select = await screen.findByRole("combobox");
    fireEvent.change(select, { target: { value: "opaas_ai:odoo-gpt" } });

    const providerInput = screen.getByPlaceholderText(/provider/i) as HTMLInputElement;
    expect(providerInput.value).toBe("opaas_ai");
    expect(providerInput).toHaveAttribute("readonly");

    const spinbuttons = screen.getAllByRole("spinbutton");
    fireEvent.change(spinbuttons[0], { target: { value: "1" } });
    fireEvent.change(spinbuttons[1], { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() =>
      expect(postPrice).toHaveBeenCalledWith({
        provider: "opaas_ai",
        modelPattern: "opaas_ai:odoo-gpt",
        priceInUsdPer1M: 1,
        priceOutUsdPer1M: 2,
      }),
    );
  });

  it("'Enter manually' falls back to free-text fields for both model and provider", async () => {
    getPrices.mockResolvedValue([]);
    getModels.mockResolvedValue(REGISTERED_MODELS);
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /add model/i }));
    await screen.findByRole("combobox");
    fireEvent.click(screen.getByRole("button", { name: /enter manually/i }));

    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    const providerInput = screen.getByPlaceholderText(/provider/i) as HTMLInputElement;
    expect(providerInput).not.toHaveAttribute("readonly");
  });

  it("falls back to manual entry with no registered models at all (existing behavior preserved)", async () => {
    getPrices.mockResolvedValue([]);
    getModels.mockResolvedValue([]);
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /add model/i }));

    expect(await screen.findByPlaceholderText(/model pattern/i)).toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });
});
