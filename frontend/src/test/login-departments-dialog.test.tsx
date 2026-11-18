import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LanguageProvider } from "@/lib/i18n";

const saveMock = vi.fn();
vi.mock("sonner", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));
vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useDepartments: () => ({
      data: {
        items: [
          { id: "d1", name: "Sales" },
          { id: "d2", name: "Support" },
        ],
      },
    }),
  };
});
vi.mock("@/lib/connections", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/connections")>();
  return {
    ...actual,
    useSetCredentialDepartments: () => ({ mutate: saveMock, isPending: false }),
  };
});

import { LoginDepartmentsDialog } from "@/components/connections/organisation-credentials";

function renderDialog() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <LanguageProvider>
        <LoginDepartmentsDialog
          login={{
            id: "c1",
            name: "Company login",
            departmentIds: ["d1"],
            allDepartments: false,
            usedBy: { departments: ["d1"], agents: ["a1"] },
          }}
          onOpenChange={vi.fn()}
        />
      </LanguageProvider>
    </QueryClientProvider>,
  );
}

describe("LoginDepartmentsDialog", () => {
  beforeEach(() => saveMock.mockReset());

  it("starts from the saved departments and saves the new choice", () => {
    renderDialog();
    expect(screen.getByText("used by 1 agent · used in 1 department")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Sales" })).toBeChecked();
    fireEvent.click(screen.getByRole("checkbox", { name: "Support" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(saveMock).toHaveBeenCalledWith(
      { id: "c1", departmentIds: ["d1", "d2"], allDepartments: false },
      expect.anything(),
    );
  });

  it("enables a login for all departments", () => {
    renderDialog();
    fireEvent.click(screen.getByRole("checkbox", { name: "All departments" }));
    expect(screen.getByRole("checkbox", { name: "Support" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(saveMock).toHaveBeenCalledWith(
      { id: "c1", departmentIds: [], allDepartments: true },
      expect.anything(),
    );
  });
});
