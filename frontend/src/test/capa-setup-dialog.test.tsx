import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import type { DiscoveredCapa } from "@/lib/hooks";
import { LanguageProvider } from "@/lib/i18n";

const configureMock = vi.fn();
const testMock = vi.fn();
const { toastError } = vi.hoisted(() => ({ toastError: vi.fn() }));
vi.mock("sonner", () => ({ toast: { error: toastError, success: vi.fn() } }));

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useConfigurePlugin: () => ({ mutateAsync: configureMock, isPending: false }),
    useTestMcpConnectionById: () => ({ mutateAsync: testMock, isPending: false }),
    useDepartments: () => ({ data: { items: [{ id: "d1", name: "Vertrieb" }] } }),
    useCredentials: () => ({
      data: [
        {
          id: "c1",
          name: "Firmenzugang",
          credentialType: "svc_login",
          incomplete: [],
          departmentIds: ["d1"],
          allDepartments: false,
          usedBy: { departments: ["d1"], agents: ["a1", "a2", "a3"] },
        },
      ],
    }),
    useCredentialTypes: () => ({
      data: [
        {
          name: "svc_login",
          displayName: "Service",
          displayNameTranslations: {},
          fields: [
            { key: "url", label: "Adresse", kind: "url", required: true, default: "" },
            { key: "token", label: "Token", kind: "password", required: true, default: "" },
          ],
        },
      ],
    }),
    useCreateCredential: () => ({ mutateAsync: vi.fn(), isPending: false }),
  };
});

import { CapaSetupDialog } from "@/components/capa-setup-dialog";

const PLUGIN = {
  name: "Service",
  databaseId: "p1",
  setup: {
    title: "Set up Service",
    description: "",
    submit_label: "Save & test",
    fields: [
      { key: "department", label: "Department", kind: "department", required: false },
      {
        key: "system",
        label: "Login",
        kind: "credential",
        required: true,
        credential_type: "svc_login",
      },
    ],
    mcp: { connection_key: "primary" },
  },
} as unknown as DiscoveredCapa;

function renderDialog() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <LanguageProvider>
        <CapaSetupDialog plugin={PLUGIN} onClose={vi.fn()} />
      </LanguageProvider>
    </QueryClientProvider>,
  );
}

describe("CapaSetupDialog", () => {
  beforeEach(() => {
    window.localStorage.setItem("oc8-lang", "de");
    configureMock.mockReset();
    testMock.mockReset();
    toastError.mockReset();
  });

  it("shows where a chosen login is enabled and how many agents use it", () => {
    renderDialog();
    fireEvent.change(screen.getByRole("combobox", { name: /login/i }), {
      target: { value: "c1" },
    });
    expect(
      screen.getByText("Für 1 Abteilung(en) freigegeben · von 3 Agenten genutzt"),
    ).toBeInTheDocument();
  });

  it("marks the fields of an incomplete login the server refused", async () => {
    configureMock.mockRejectedValue(
      new ApiError("incomplete", 422, {
        detail: { code: "credential_incomplete", missing: ["url", "token"] },
      }),
    );
    renderDialog();
    fireEvent.change(screen.getByRole("combobox", { name: /login/i }), {
      target: { value: "c1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save & test" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Firmenzugang ist unvollständig: Adresse, Token fehlt",
    );
    expect(screen.getByRole("combobox", { name: /login/i })).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(toastError).toHaveBeenCalledWith(
      "Der gewählte Zugang ist unvollständig. Vervollständige ihn zuerst.",
    );
    expect(testMock).not.toHaveBeenCalled();
  });
});
