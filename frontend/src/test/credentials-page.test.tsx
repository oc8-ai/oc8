import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";

const credentialsMock = vi.fn();
const credentialTypesMock = vi.fn();
const deleteCredentialMock = vi.fn();
const testCredentialMock = vi.fn();
const updateCredentialMock = vi.fn();
const mayMock = vi.fn();
const { toastError, toastSuccess } = vi.hoisted(() => ({
  toastError: vi.fn(),
  toastSuccess: vi.fn(),
}));

vi.mock("sonner", () => ({ toast: { error: toastError, success: toastSuccess } }));

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

// The row's actions sit in an overflow menu; Radix opens it on pointerdown.
function openActions() {
  fireEvent.pointerDown(screen.getByRole("button", { name: "Actions for Prod S3" }), { button: 0 });
}
function choose(name: RegExp) {
  openActions();
  fireEvent.click(screen.getByRole("menuitem", { name }));
}

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
    toastError.mockReset();
    toastSuccess.mockReset();
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
    choose(/^test$/i);
    expect(testCredentialMock).toHaveBeenCalledWith("c1", expect.anything());
  });

  it("delete asks for confirmation before calling useDeleteCredential", async () => {
    renderPage();
    choose(/^delete$/i);
    expect(deleteCredentialMock).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: /^delete$/i }));
    await vi.waitFor(() =>
      expect(deleteCredentialMock).toHaveBeenCalledWith("c1", expect.anything()),
    );
  });

  it("delete does nothing when the confirmation dialog is cancelled", async () => {
    renderPage();
    choose(/^delete$/i);
    fireEvent.click(await screen.findByRole("button", { name: /^cancel$/i }));
    // Flush the confirm dialog's resolved promise before asserting a
    // negative -- there is nothing observable to `waitFor` on here.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(deleteCredentialMock).not.toHaveBeenCalled();
  });

  it("edit opens a prefilled dialog and saves the new name", () => {
    renderPage();
    choose(/^edit$/i);
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
    choose(/^edit$/i);
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
    expect(screen.queryByRole("button", { name: /actions for/i })).not.toBeInTheDocument();
  });

  it("is a titled section of flat rows, no table and no card", () => {
    renderPage();
    const section = screen.getByRole("region", { name: "Further credentials" });
    expect(section.querySelector("table")).toBeNull();
    expect(screen.getByRole("listitem")).toHaveTextContent("Prod S3");
    expect(screen.getByRole("listitem")).toHaveTextContent("Never tested");
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

  describe("never shows the server's own error text", () => {
    const raw = new ApiError("psycopg.errors.UniqueViolation: duplicate key", 500);

    it("when a test fails", () => {
      testCredentialMock.mockImplementation(
        (_id: string, opts: { onError: (e: unknown) => void }) => opts.onError(raw),
      );
      renderPage();
      choose(/^test$/i);
      expect(toastError).toHaveBeenCalledWith("The check failed. Please try again.");
    });

    it("when a test reports the credential does not work", () => {
      testCredentialMock.mockImplementation(
        (_id: string, opts: { onSuccess: (r: { ok: boolean }) => void }) =>
          opts.onSuccess({ ok: false }),
      );
      renderPage();
      choose(/^test$/i);
      expect(toastSuccess).not.toHaveBeenCalled();
      expect(toastError).toHaveBeenCalledWith(
        "The connection did not work. Please check your entries and try again.",
      );
    });

    it("when deleting fails", async () => {
      deleteCredentialMock.mockImplementation(
        (_id: string, opts: { onError: (e: unknown) => void }) => opts.onError(raw),
      );
      renderPage();
      choose(/^delete$/i);
      fireEvent.click(await screen.findByRole("button", { name: /^delete$/i }));
      await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith("Could not delete."));
    });

    it("when saving is not allowed", () => {
      updateCredentialMock.mockImplementation(
        (_body: unknown, opts: { onError: (e: unknown) => void }) =>
          opts.onError(new ApiError("Forbidden: missing secret:manage on tenant 42", 403)),
      );
      renderPage();
      choose(/^edit$/i);
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
      expect(toastError).toHaveBeenCalledWith("You are not allowed to do this.");
    });
  });
});
