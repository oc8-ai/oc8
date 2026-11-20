import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi, beforeEach } from "vitest";
import type { ModelProviderDTO } from "@/lib/hooks";

const modelsMock = vi.fn();
const providersMock = vi.fn();

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useModels: () => modelsMock(),
    useModelProviders: () => providersMock(),
    useCredentials: () => ({ data: [] }),
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

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ModelsPage />
    </QueryClientProvider>,
  );
}

describe("Connected LLMs", () => {
  beforeEach(() => {
    modelsMock.mockReset().mockReturnValue({ data: [] });
  });

  it("shows only the providers the tenant has connected", () => {
    const providers: ModelProviderDTO[] = [
      { canonical: "ollama", locality: "local", available: true, connected: false },
      { canonical: "openai_chatgpt", locality: "cloud", available: true, connected: false },
      { canonical: "opaas_ai", locality: "cloud", available: true, connected: true },
    ];
    providersMock.mockReset().mockReturnValue({ data: providers });

    renderPage();

    expect(screen.getByText("opaas_ai")).toBeInTheDocument();
    expect(screen.queryByText("ollama")).toBeNull();
    expect(screen.queryByText("openai_chatgpt")).toBeNull();
  });
});
