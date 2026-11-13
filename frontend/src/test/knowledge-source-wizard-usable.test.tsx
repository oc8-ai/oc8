// The data-source picker shows a connector that cannot be set up yet (it signs
// in somewhere and the organisation has no connection of that provider) as a
// disabled tile with a "Connect first" link to /connections, instead of
// letting the user pick it and stall on step 2.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return {
    ...actual,
    Link: ({
      children,
      to,
      className,
    }: {
      children?: ReactNode;
      to?: string;
      className?: string;
    }) => (
      <a href={to} className={className}>
        {children}
      </a>
    ),
  };
});

vi.mock("@/lib/hooks", () => ({
  useKnowledgeBases: () => KBS,
  useCreateKnowledgeBase: () => ({ mutate: vi.fn(), isPending: false }),
  useEmbeddingModels: () => ({ data: [] }),
  useCredentials: () => ({ data: [] }),
  useCredentialTypes: () => ({ data: [] }),
  useCreateCredential: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

const KBS = { data: { items: [{ id: "kb-1", name: "KB" }], totalCount: 1 } };
const CONNECTORS = {
  data: [
    {
      typeId: "google_workspace_files",
      label: "Google Drive",
      configSchema: { properties: {} },
      requiresOauth: "google",
      usable: false,
      missing: "oauth:google",
    },
    {
      typeId: "website",
      label: "Website",
      configSchema: { properties: {} },
      requiresOauth: null,
      usable: true,
      missing: null,
    },
  ],
  isLoading: false,
};
const NO_OAUTH = { data: [] };

vi.mock("@/lib/knowledge-connector-hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/knowledge-connector-hooks")>();
  return {
    ...actual,
    useKnowledgeConnectors: () => CONNECTORS,
    useOAuthConnections: () => NO_OAUTH,
    useCreateSource: () => ({ mutate: vi.fn(), isPending: false }),
    useEmbeddingModels: () => ({ data: [] }),
  };
});

import { SourceWizard } from "@/routes/knowledge";

function renderWizard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SourceWizard onClose={vi.fn()} onCreate={vi.fn()} />
    </QueryClientProvider>,
  );
}

describe("SourceWizard: connectors that need a connection first", () => {
  it("shows an unusable connector disabled with a link to /connections", () => {
    renderWizard();
    const google = screen.getByRole("button", { name: /google drive/i });
    expect(google).toBeDisabled();
    const link = screen.getByRole("link", { name: /connect first/i });
    expect(link).toHaveAttribute("href", "/connections");
    expect(screen.getByRole("button", { name: /website/i })).toBeEnabled();
  });

  it("starts on a usable connector, not the unusable first one", () => {
    renderWizard();
    fireEvent.click(screen.getByRole("button", { name: /^continue$/i }));
    expect(screen.getByText(/Connect · Website/)).toBeInTheDocument();
  });
});
