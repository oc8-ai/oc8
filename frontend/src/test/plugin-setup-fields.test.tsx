import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PluginSetupSpec } from "@/lib/hooks";

// `PluginSetupFields` is the shared field-kind switch that both
// `CapaSetupDialog` and the onboarding wizard's tool-connect step render
// through, so a kind="credential" field is exercised here rather than via a
// (non-existent) capa-setup-dialog test file -- see task-11-report.md.
const credentialsMock = vi.fn();
const credentialTypesMock = vi.fn();

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useCredentials: (type?: string) => credentialsMock(type),
    useCredentialTypes: () => credentialTypesMock(),
  };
});

import { PluginSetupFields } from "@/components/plugin-setup-fields";

const setup: PluginSetupSpec = {
  title: "Connect Odoo",
  description: "",
  submit_label: "Connect",
  fields: [
    { key: "url", label: "Server URL", kind: "url", required: true, default: "", placeholder: "" },
  ],
  mcp: null,
} as unknown as PluginSetupSpec;

function renderWithClient(ui: React.ReactElement) {
  const qc = new QueryClient();
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("PluginSetupFields", () => {
  beforeEach(() => {
    credentialsMock.mockReset();
    credentialTypesMock.mockReset();
    credentialsMock.mockReturnValue({ data: [] });
    credentialTypesMock.mockReturnValue({ data: [] });
  });

  it("reports typed field values back through onChange", () => {
    const onChange = vi.fn();
    renderWithClient(<PluginSetupFields setup={setup} values={{}} onChange={onChange} />);
    const url = screen.getByLabelText(/^server url/i);
    // Required, marked, and with a placeholder that cannot pass for a value.
    expect(url).toBeRequired();
    expect(url).toHaveAttribute("placeholder", "https://your-company.example.com");
    fireEvent.change(url, {
      target: { value: "https://example.com" },
    });
    expect(onChange).toHaveBeenCalledWith({ url: "https://example.com" });
  });

  it("renders a CredentialPicker for a field of kind 'credential'", () => {
    const credentialSetup: PluginSetupSpec = {
      title: "Connect Telegram",
      description: "",
      submit_label: "Connect",
      fields: [
        {
          key: "bot_token",
          label: "Bot token",
          kind: "credential",
          required: true,
          default: "",
          placeholder: "",
          credential_type: "telegram_bot",
        },
      ],
      mcp: null,
    } as unknown as PluginSetupSpec;

    const { container } = renderWithClient(
      <PluginSetupFields setup={credentialSetup} values={{}} onChange={vi.fn()} />,
    );

    expect(screen.getByRole("button", { name: /create new/i })).toBeInTheDocument();
    expect(container.querySelector('input[type="password"]')).not.toBeInTheDocument();
    expect(credentialsMock).toHaveBeenCalledWith("telegram_bot");
  });

  it("renders a dropdown for a field of kind 'select' and reports the chosen option", () => {
    const selectSetup: PluginSetupSpec = {
      title: "Connect Telegram",
      description: "",
      submit_label: "Connect",
      fields: [
        {
          key: "max_classification",
          label: "Höchste erlaubte Vertraulichkeit",
          kind: "select",
          required: false,
          default: "public",
          placeholder: "",
          options: ["public", "internal", "confidential", "restricted"],
        },
      ],
      mcp: null,
    } as unknown as PluginSetupSpec;

    const onChange = vi.fn();
    renderWithClient(<PluginSetupFields setup={selectSetup} values={{}} onChange={onChange} />);

    const select = screen.getByLabelText("Höchste erlaubte Vertraulichkeit") as HTMLSelectElement;
    expect(select.value).toBe("public");
    expect(Array.from(select.options).map((option) => option.value)).toEqual([
      "public",
      "internal",
      "confidential",
      "restricted",
    ]);

    fireEvent.change(select, { target: { value: "internal" } });
    expect(onChange).toHaveBeenCalledWith({ max_classification: "internal" });
  });
});
