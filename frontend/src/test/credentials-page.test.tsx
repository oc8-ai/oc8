import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const credentialsMock = vi.fn();
const credentialTypesMock = vi.fn();
const deleteCredentialMock = vi.fn();
const testCredentialMock = vi.fn();
const updateCredentialMock = vi.fn();
const mayMock = vi.fn();

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useCredentials: () => credentialsMock(),
    useCredentialTypes: () => credentialTypesMock(),
    useCreateCredential: () => ({ mutateAsync: vi.fn(), isPending: false }),
    useUpdateCredential: () => ({ mutate: updateCredentialMock, isPending: false }),
    useDeleteCredential: () => ({ mutate: deleteCredentialMock, isPending: false }),
    useTestCredential: () => ({ mutate: testCredentialMock, isPending: false }),
  };
});
vi.mock("@/lib/governance-hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/governance-hooks")>();
  return { ...actual, useMay: () => mayMock };
});

import { OrganisationCredentials as CredentialsPage } from "@/components/connections/organisation-credentials";

function renderPage(excludeTypes?: string[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <CredentialsPage excludeTypes={excludeTypes} />
    </QueryClientProvider>,
  );
}

describe("Further credentials (organisation)", () => {
  beforeEach(() => {
    credentialsMock.mockReset();
    credentialTypesMock.mockReset();
    deleteCredentialMock.mockReset();
    testCredentialMock.mockReset();
    updateCredentialMock.mockReset();
    mayMock.mockReset();
    mayMock.mockReturnValue(true);
    credentialsMock.mockReturnValue({
      data: [
        {
          id: "c1",
          name: "Prod S3",
          credentialType: "s3_api",
          fieldValues: {},
          lastTestedAt: null,
          lastTestOk: null,
        },
      ],
    });
    credentialTypesMock.mockReturnValue({
      data: [{ name: "s3_api", displayName: "S3", fields: [] }],
    });
  });

  it("lists existing credentials with their type", () => {
    renderPage();
    expect(screen.getByText("Prod S3")).toBeInTheDocument();
    expect(screen.getByText("S3")).toBeInTheDocument();
  });

  it("test button calls useTestCredential", () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /test/i }));
    expect(testCredentialMock).toHaveBeenCalledWith("c1", expect.anything());
  });

  it("delete asks for confirmation before calling useDeleteCredential", async () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /^delete$/i }));
    expect(deleteCredentialMock).not.toHaveBeenCalled();
    // The confirm dialog's own action button shares the "Delete" label with
    // the row's trigger button, so two matches exist once it's open.
    const deleteButtons = screen.getAllByRole("button", { name: /^delete$/i });
    fireEvent.click(deleteButtons[deleteButtons.length - 1]);
    await vi.waitFor(() =>
      expect(deleteCredentialMock).toHaveBeenCalledWith("c1", expect.anything()),
    );
  });

  it("delete does nothing when the confirmation dialog is cancelled", async () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /^delete$/i }));
    fireEvent.click(screen.getByRole("button", { name: /^cancel$/i }));
    // Flush the confirm dialog's resolved promise before asserting a
    // negative -- there is nothing observable to `waitFor` on here.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(deleteCredentialMock).not.toHaveBeenCalled();
  });

  it("edit opens a prefilled dialog and saves the new name", () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /edit/i }));
    const nameInput = screen.getByDisplayValue("Prod S3");
    fireEvent.change(nameInput, { target: { value: "Prod S3 (renamed)" } });
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    expect(updateCredentialMock).toHaveBeenCalledWith(
      { credentialId: "c1", name: "Prod S3 (renamed)", fieldValues: {} },
      expect.anything(),
    );
  });

  it("edit leaves an untouched password field out of the submitted fieldValues", () => {
    credentialTypesMock.mockReturnValue({
      data: [
        {
          name: "s3_api",
          displayName: "S3",
          fields: [
            {
              key: "access_key",
              label: "Access key",
              kind: "password",
              required: false,
              default: "",
              placeholder: "",
              help: "",
            },
          ],
        },
      ],
    });
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /edit/i }));
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    expect(updateCredentialMock).toHaveBeenCalledWith(
      { credentialId: "c1", fieldValues: {} },
      expect.anything(),
    );
  });

  it("a secret:view-only user sees the list but not the manage affordances", () => {
    mayMock.mockImplementation((permission: string) => permission === "secret:view");
    renderPage();
    expect(screen.getByText("Prod S3")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /new credential/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /test/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /edit/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /delete/i })).not.toBeInTheDocument();
  });

  it("is titled further credentials", () => {
    renderPage();
    expect(screen.getByRole("heading", { name: "Further credentials" })).toBeInTheDocument();
  });

  it("leaves out the types an integration already lists", () => {
    credentialsMock.mockReturnValue({
      data: [
        {
          id: "c1",
          name: "Prod S3",
          credentialType: "s3_api",
          fieldValues: {},
          lastTestedAt: null,
          lastTestOk: null,
        },
        {
          id: "c2",
          name: "Odoo system",
          credentialType: "odoo_login",
          fieldValues: {},
          lastTestedAt: null,
          lastTestOk: null,
        },
      ],
    });
    renderPage(["odoo_login"]);
    expect(screen.getByText("Prod S3")).toBeInTheDocument();
    expect(screen.queryByText("Odoo system")).not.toBeInTheDocument();
  });

  it("renders nothing without secret:view", () => {
    mayMock.mockReturnValue(false);
    const { container } = renderPage();
    expect(container).toBeEmptyDOMElement();
  });
});
