import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LanguageProvider } from "@/lib/i18n";

const get = vi.fn();
const patch = vi.fn();

vi.mock("sonner", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, get: (p: string) => get(p), patch: (p: string, b: unknown) => patch(p, b) } };
});
vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return { ...actual, useCapaIcon: () => ({ data: null }) };
});
vi.mock("@/lib/connections", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/connections")>();
  return { ...actual, useIntegrations: () => ({ data: [ODOO], isLoading: false }) };
});
vi.mock("@/lib/governance-hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/governance-hooks")>();
  return { ...actual, useMay: () => () => true };
});

import { ConnectDialog } from "@/components/connections/connect-dialog";

const ODOO = {
  name: "odoo_mcp",
  label: "Odoo",
  summary: "",
  toolKey: "odoo",
  kind: "credential",
  provider: null,
  credentialType: "odoo_login",
  available: true,
  capaId: "c2",
  credentialFields: [
    { key: "url", label: "Odoo URL", kind: "url", required: true },
    { key: "database", label: "Database", kind: "text", default: "main" },
    { key: "username", label: "Odoo user", kind: "text" },
    { key: "password", label: "Odoo password", kind: "password", required: true },
  ],
};
const STORED = {
  id: "cred-1",
  name: "Odoo",
  credentialType: "odoo_login",
  fieldValues: { url: "https://odoo.acme.test", database: "acme", username: "anna" },
};

function renderEdit(owner: "me" | "organisation") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <LanguageProvider>
        <ConnectDialog
          integration="odoo_mcp"
          defaultOwner={owner}
          editCredentialId="cred-1"
          open
          onOpenChange={vi.fn()}
        />
      </LanguageProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  get.mockReset();
  patch.mockReset();
  patch.mockResolvedValue({});
  get.mockImplementation(async (path: string) =>
    path.startsWith("/credentials") ? [{ ...STORED, id: "other" }, STORED] : STORED,
  );
});

function fillRequired() {
  fireEvent.change(screen.getByLabelText(/Odoo URL/), { target: { value: "https://odoo.acme.test" } });
}

describe("saving an edited login", () => {
  it("editing an organisation login saves through the organisation route", async () => {
    renderEdit("organisation");
    await screen.findByRole("button", { name: "Save" });
    fillRequired();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    expect(patch.mock.calls[0][0]).toBe("/credentials/cred-1");
  });

  it("editing a personal login saves through the personal route", async () => {
    renderEdit("me");
    await screen.findByRole("button", { name: "Save" });
    fillRequired();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    expect(patch.mock.calls[0][0]).toBe("/connections/credentials/cred-1");
  });
});
