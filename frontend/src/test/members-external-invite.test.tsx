// A deployment with an external member inviter (GET /auth/config
// memberInvites: "external") invites by email with a chosen role and no
// password; a refusal shows the deployment's message in the dialog, with the
// action link it sends along.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";

const createMemberMutateMock = vi.fn();
const authConfig = vi.fn();

vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return {
    ...actual,
    Link: ({ children, className }: { children?: ReactNode; className?: string }) => (
      <a className={className}>{children}</a>
    ),
  };
});

vi.mock("@/lib/roles-hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/roles-hooks")>();
  return {
    ...actual,
    useAssignees: () => ({ data: { items: [], totalCount: 0 }, isLoading: false, error: null }),
    useCreateMember: () => ({ mutate: createMemberMutateMock, isPending: false }),
  };
});

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return { ...actual, useAuthConfig: () => ({ data: authConfig() }) };
});

vi.mock("@/lib/governance-hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/governance-hooks")>();
  return { ...actual, useMay: () => () => true };
});

import { MembersPage } from "@/routes/members.index";

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MembersPage />
    </QueryClientProvider>,
  );
}

describe("Inviting members through an external inviter", () => {
  beforeEach(() => {
    createMemberMutateMock.mockReset();
    authConfig.mockReturnValue({ mode: "community", memberInvites: "external" });
  });

  it("asks for a role, not a password, and sends the role", () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /new user/i }));

    expect(screen.getByLabelText(/role/i)).toHaveValue("member");
    expect(screen.queryByText(/password/i)).toBeNull();
    fireEvent.change(screen.getByPlaceholderText("jane.doe@example.com"), {
      target: { value: "kim@acme.test" },
    });
    fireEvent.change(screen.getByLabelText(/role/i), { target: { value: "operator" } });
    fireEvent.click(screen.getByRole("button", { name: /^invite$/i }));

    expect(createMemberMutateMock.mock.calls[0][0]).toMatchObject({
      subject: "kim@acme.test",
      role: "operator",
    });
  });

  it("shows a refusal with the action the deployment sends", async () => {
    createMemberMutateMock.mockImplementation((_body, options) =>
      options.onError(
        new ApiError(
          JSON.stringify({
            code: "limit_reached",
            message: "This team is full.",
            actionLabel: "Upgrade",
            actionUrl: "/upgrade",
          }),
          409,
        ),
      ),
    );
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /new user/i }));
    fireEvent.change(screen.getByPlaceholderText("jane.doe@example.com"), {
      target: { value: "kim@acme.test" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^invite$/i }));

    expect(await screen.findByText(/This team is full\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Upgrade" })).toHaveAttribute("href", "/upgrade");
  });

  it("never follows an action link that leaves the app", async () => {
    createMemberMutateMock.mockImplementation((_body, options) =>
      options.onError(
        new ApiError(
          JSON.stringify({
            code: "limit_reached",
            message: "This team is full.",
            actionLabel: "Upgrade",
            actionUrl: "javascript:alert(1)",
          }),
          409,
        ),
      ),
    );
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /new user/i }));
    fireEvent.change(screen.getByPlaceholderText("jane.doe@example.com"), {
      target: { value: "kim@acme.test" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^invite$/i }));

    expect(await screen.findByText(/This team is full\./)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Upgrade" })).toBeNull();
  });

  it("keeps the local invite unchanged without an external inviter", () => {
    authConfig.mockReturnValue({ mode: "community" });
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /new user/i }));

    expect(screen.queryByLabelText(/role/i)).toBeNull();
    expect(screen.getByRole("button", { name: /^create user$/i })).toBeInTheDocument();
  });
});
