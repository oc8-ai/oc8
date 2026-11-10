// Support has to know which organisation a person writes from, and only an
// administrator sees the organisation's settings. The profile, which every
// member has, shows the organisation id with a copy button.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const TENANT = "0b6f7c1e-2d4a-4f8e-9c35-7a1d2e8b4f60";

const { authConfigMock, meMock, langRef } = vi.hoisted(() => ({
  authConfigMock: vi.fn(() => ({ mode: "community" }) as Record<string, unknown>),
  meMock: vi.fn(() => ({ subject: "ana@acme.test", memberId: "m-1" }) as Record<string, unknown>),
  langRef: { current: "en" as "en" | "de" },
}));

vi.mock("@/lib/i18n", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/i18n")>();
  return {
    ...actual,
    useT: () => (en: string, de?: string) => (langRef.current === "de" ? (de ?? en) : en),
  };
});

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useAuthConfig: () => ({ data: authConfigMock() }),
    useAuth: () => ({ data: meMock() }),
  };
});

import { AccountPanel } from "@/routes/profile";

function wrap(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>);
}

beforeEach(() => {
  langRef.current = "en";
  authConfigMock.mockReset();
  authConfigMock.mockReturnValue({ mode: "community" });
  meMock.mockReset();
  meMock.mockReturnValue({ subject: "ana@acme.test", memberId: "m-1", tenantId: TENANT });
});

describe("Profile: organisation id", () => {
  it("shows the organisation id to every member, with a support hint", () => {
    wrap(<AccountPanel initialDisplayName="Ana" />);

    expect(screen.getByTestId("organisation-id")).toHaveTextContent(TENANT);
    expect(screen.getByText(/include it when you contact support/i)).toBeInTheDocument();
  });

  it("shows it with an external identity provider too", () => {
    authConfigMock.mockReturnValue({
      mode: "community",
      memberInvites: "external",
      accountManagementUrl: "https://id.example.test/account",
    });
    wrap(<AccountPanel initialDisplayName="Ana" />);

    expect(screen.getByTestId("organisation-id")).toHaveTextContent(TENANT);
  });

  it("copies the id", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    wrap(<AccountPanel initialDisplayName="Ana" />);

    fireEvent.click(screen.getByRole("button", { name: /copy organisation id/i }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(TENANT));
  });

  it("shows nothing while the caller is still loading", () => {
    meMock.mockReturnValue({ subject: "ana@acme.test", memberId: "m-1" });
    wrap(<AccountPanel initialDisplayName="Ana" />);

    expect(screen.queryByTestId("organisation-id")).toBeNull();
  });

  it("speaks German with du", () => {
    langRef.current = "de";
    wrap(<AccountPanel initialDisplayName="Ana" />);

    expect(screen.getByText(/Organisations-ID/)).toBeInTheDocument();
    expect(screen.getByText("Gib sie bei Support-Anfragen an.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Organisations-ID kopieren" })).toBeInTheDocument();
  });
});
