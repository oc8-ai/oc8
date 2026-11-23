import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LanguageProvider } from "@/lib/i18n";

const credentialsMock = vi.fn();
const credentialTypesMock = vi.fn();
const createCredentialMock = vi.fn();

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useCredentials: (type?: string) => credentialsMock(type),
    useCredentialTypes: () => credentialTypesMock(),
    useCreateCredential: () => ({ mutateAsync: createCredentialMock, isPending: false }),
  };
});

import { CredentialPicker, URL_PLACEHOLDER } from "@/components/credential-picker";
import { ApiError } from "@/lib/api";

function renderPicker(props: Partial<React.ComponentProps<typeof CredentialPicker>> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <LanguageProvider>
        <CredentialPicker credentialType="s3_api" value="" onChange={vi.fn()} {...props} />
      </LanguageProvider>
    </QueryClientProvider>,
  );
}

const S3_TYPE = {
  name: "s3_api",
  displayName: "S3 / Object storage",
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
};

describe("CredentialPicker", () => {
  beforeEach(() => {
    credentialsMock.mockReset();
    credentialTypesMock.mockReset();
    createCredentialMock.mockReset();
    credentialsMock.mockReturnValue({
      data: [{ id: "c1", name: "Prod S3", credentialType: "s3_api" }],
    });
    credentialTypesMock.mockReturnValue({ data: [S3_TYPE] });
  });

  it("lists existing credentials of the given type", () => {
    renderPicker();
    expect(screen.getByText("Prod S3")).toBeInTheDocument();
  });

  it("calls onChange when an existing credential is selected", () => {
    const onChange = vi.fn();
    renderPicker({ onChange });
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "c1" } });
    expect(onChange).toHaveBeenCalledWith("c1");
  });

  it("creating a new credential calls onChange with its new id", async () => {
    createCredentialMock.mockResolvedValue({ id: "new-id" });
    const onChange = vi.fn();
    renderPicker({ onChange });
    fireEvent.click(screen.getByRole("button", { name: /create new/i }));
    fireEvent.change(screen.getByLabelText("Access key", { exact: false }), {
      target: { value: "AKIA_X" },
    });
    fireEvent.change(screen.getByLabelText(/name/i), { target: { value: "New credential" } });
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    await waitFor(() => {
      expect(createCredentialMock).toHaveBeenCalledWith({
        name: "New credential",
        credentialType: "s3_api",
        fieldValues: { access_key: "AKIA_X" },
      });
      expect(onChange).toHaveBeenCalledWith("new-id");
    });
  });

  it("resolves credential_type display_name/field prose from *_translations under the German locale", () => {
    credentialTypesMock.mockReturnValue({
      data: [
        {
          ...S3_TYPE,
          displayNameTranslations: { de: "S3 / Objektspeicher" },
          fields: [
            {
              ...S3_TYPE.fields[0],
              label_translations: { de: "Zugriffsschlüssel" },
              help: "Found in the provider console.",
              help_translations: { de: "Zu finden in der Anbieterkonsole." },
            },
          ],
        },
      ],
    });
    window.localStorage.setItem("oc8-lang", "de");
    try {
      renderPicker();
      fireEvent.click(screen.getByRole("button", { name: /neu erstellen/i }));
      expect(screen.getByText("Zugriffsschlüssel")).toBeInTheDocument();
      expect(screen.getByText("Zu finden in der Anbieterkonsole.")).toBeInTheDocument();
      expect(screen.queryByText("Access key")).not.toBeInTheDocument();
    } finally {
      window.localStorage.removeItem("oc8-lang");
    }
  });

  it("renders a select field as a select of its choices (E2E F15)", async () => {
    // A mailbox's protocol and encryption were free-text boxes, so "IMAP"
    // or "tls" could be typed where only `imap`/`pop3`, `ssl`/`starttls` work.
    credentialTypesMock.mockReturnValue({
      data: [
        {
          name: "mail_account",
          displayName: "Mailbox",
          fields: [
            {
              key: "incoming_security",
              label: "Incoming encryption",
              kind: "select",
              options: ["ssl", "starttls"],
              required: true,
              default: "ssl",
              placeholder: "",
              help: "",
            },
          ],
        },
      ],
    });
    credentialsMock.mockReturnValue({ data: [] });
    createCredentialMock.mockResolvedValue({ id: "new-id" });
    renderPicker({ credentialType: "mail_account" });
    fireEvent.click(screen.getByRole("button", { name: /create new/i }));
    const security = screen.getByLabelText(/incoming encryption/i) as HTMLSelectElement;
    expect(security.tagName).toBe("SELECT");
    expect(Array.from(security.options).map((o) => o.value)).toEqual(["ssl", "starttls"]);
    expect(security.value).toBe("ssl");
    fireEvent.change(security, { target: { value: "starttls" } });
    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "Inbox" } });
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    await waitFor(() =>
      expect(createCredentialMock).toHaveBeenCalledWith({
        name: "Inbox",
        credentialType: "mail_account",
        fieldValues: { incoming_security: "starttls" },
      }),
    );
  });

  describe("a login with a required URL", () => {
    const LOGIN_TYPE = {
      name: "svc_login",
      displayName: "Service",
      fields: [
        {
          key: "url",
          label: "Address",
          kind: "url",
          required: true,
          default: "",
          // What a pack once shipped: it looked like a value already filled in.
          placeholder: "http://host.docker.internal:1000",
          help: "",
        },
        {
          key: "database",
          label: "Database",
          kind: "text",
          required: true,
          default: "main",
          placeholder: "",
          help: "",
        },
      ],
    };

    beforeEach(() => {
      credentialTypesMock.mockReturnValue({ data: [LOGIN_TYPE] });
      credentialsMock.mockReturnValue({ data: [] });
    });

    it("blocks saving when a required field is empty", async () => {
      createCredentialMock.mockResolvedValue({ id: "new-id" });
      renderPicker({ credentialType: "svc_login" });
      fireEvent.click(screen.getByRole("button", { name: /create new/i }));
      fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "Prod" } });

      const url = screen.getByLabelText(/address/i) as HTMLInputElement;
      // The placeholder can no longer pass for a value, and the field is empty.
      expect(url.value).toBe("");
      expect(url.placeholder).toBe(URL_PLACEHOLDER);
      expect(url.placeholder).not.toMatch(/localhost|docker\.internal|127\.0\.0\.1/);
      expect(url).toBeRequired();
      expect(url).toHaveAttribute("aria-invalid", "true");
      // Marked with a "*" drawn by CSS, so the field's label stays its name.
      expect(screen.getByText("Address").querySelector("[data-required-mark]")).not.toBeNull();

      const save = screen.getByRole("button", { name: /^save$/i });
      expect(save).toBeDisabled();
      fireEvent.click(save);
      expect(createCredentialMock).not.toHaveBeenCalled();

      fireEvent.change(url, { target: { value: "https://svc.acme.test" } });
      expect(url).not.toHaveAttribute("aria-invalid");
      expect(save).toBeEnabled();
      fireEvent.click(save);
      await waitFor(() =>
        // The default the form shows is the value sent.
        expect(createCredentialMock).toHaveBeenCalledWith({
          name: "Prod",
          credentialType: "svc_login",
          fieldValues: { url: "https://svc.acme.test", database: "main" },
        }),
      );
    });

    it("marks the fields the server names as missing", async () => {
      createCredentialMock.mockRejectedValue(
        new ApiError("incomplete", 422, {
          detail: { code: "credential_incomplete", missing: ["url"] },
        }),
      );
      const onChange = vi.fn();
      renderPicker({ credentialType: "svc_login", onChange });
      fireEvent.click(screen.getByRole("button", { name: /create new/i }));
      fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "Prod" } });
      fireEvent.change(screen.getByLabelText(/address/i), { target: { value: "   x" } });
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "Prod is incomplete: Address missing",
      );
      expect(onChange).not.toHaveBeenCalled();
    });

    it("shows an incomplete login and what uses it in the list", () => {
      credentialsMock.mockReturnValue({
        data: [
          {
            id: "c1",
            name: "Old login",
            credentialType: "svc_login",
            incomplete: ["url"],
            usedBy: { departments: [], agents: ["a1", "a2"] },
          },
        ],
      });
      renderPicker({ credentialType: "svc_login", value: "c1" });
      expect(
        screen.getByRole("option", {
          name: "Old login is incomplete: Address missing · used by 2 agents",
        }),
      ).toBeInTheDocument();
      expect(screen.getByRole("combobox")).toHaveAttribute("aria-invalid", "true");
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Old login is incomplete: Address missing",
      );
    });
  });
});
