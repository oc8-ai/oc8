// The user-detail screen had no door to offboard someone, mint a fresh
// password-reset/invite link, or re-trigger the mail -- those only existed
// on create (invite) or as the public forgot-password form. Admins looking
// at /members/:id could set a password by hand but could not delete the
// person or send them a link.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mintResetMutate = vi.fn();
const deleteMutate = vi.fn();
const navigate = vi.fn();
const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}));

vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return {
    ...actual,
    Link: ({ children, className }: { children?: ReactNode; className?: string }) => (
      <a className={className}>{children}</a>
    ),
    useNavigate: () => navigate,
  };
});

vi.mock("@/lib/roles-hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/roles-hooks")>();
  return {
    ...actual,
    useRoles: () => ({ data: [] }),
    useCreateMember: () => ({ mutate: vi.fn(), isPending: false }),
    useRenameMemberSubject: () => ({ mutate: vi.fn(), isPending: false }),
    useSetMemberPassword: () => ({ mutate: vi.fn(), isPending: false }),
    useAssignRole: () => ({ mutate: vi.fn(), isPending: false }),
    useGrantSeat: () => ({ mutate: vi.fn(), isPending: false }),
    useRevokeSeat: () => ({ mutate: vi.fn(), isPending: false }),
    useMintMemberPasswordReset: () => ({ mutate: mintResetMutate, isPending: false }),
    useDeleteMember: () => ({ mutate: deleteMutate, isPending: false }),
  };
});

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useAuthConfig: () => ({ data: { mode: "community" } }),
    useDepartments: () => ({ data: { items: [] } }),
    useAuth: () => ({ data: { subject: "admin@example.com", memberId: "m-admin" } }),
  };
});

vi.mock("sonner", async (importOriginal) => {
  const actual = await importOriginal<typeof import("sonner")>();
  return { ...actual, toast: { ...actual.toast, success: toastSuccess, error: toastError } };
});

import { MemberDetailForm } from "@/routes/members.$memberId";

const MEMBER = {
  id: "m-1",
  subject: "jane@example.com",
  displayName: "Jane Doe",
};

function renderForm(member = MEMBER) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemberDetailForm member={member} />
    </QueryClientProvider>,
  );
}

type ResetResult = { resetLink: string; resetSent: boolean };

function lastMintOnSuccess(): (result: ResetResult) => void {
  const call = mintResetMutate.mock.calls.at(-1) as [
    unknown,
    { onSuccess: (result: ResetResult) => void },
  ];
  return call[1].onSuccess;
}

describe("Member detail: password-reset link", () => {
  beforeEach(() => {
    mintResetMutate.mockReset();
    deleteMutate.mockReset();
    navigate.mockReset();
    toastSuccess.mockReset();
    toastError.mockReset();
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
  });

  it("lets an admin mint a reset link from the user screen", () => {
    renderForm();

    fireEvent.click(screen.getByRole("button", { name: /send reset link/i }));

    expect(mintResetMutate).toHaveBeenCalledWith("m-1", expect.anything());
  });

  it("toasts when the reset email was sent and does not open the copy dialog", () => {
    renderForm();
    fireEvent.click(screen.getByRole("button", { name: /send reset link/i }));

    act(() => {
      lastMintOnSuccess()({
        resetLink: "https://oc8.example.test/reset-password?token=sent",
        resetSent: true,
      });
    });

    expect(toastSuccess).toHaveBeenCalledWith(expect.stringMatching(/reset email was sent/i));
    expect(screen.queryByText(/share this reset link/i)).not.toBeInTheDocument();
  });

  it("shows a copyable link when the mail could not be sent", () => {
    renderForm();
    fireEvent.click(screen.getByRole("button", { name: /send reset link/i }));

    act(() => {
      lastMintOnSuccess()({
        resetLink: "https://oc8.example.test/reset-password?token=copy-me",
        resetSent: false,
      });
    });

    expect(screen.getByText(/share this reset link/i)).toBeInTheDocument();
    expect(
      screen.getByDisplayValue("https://oc8.example.test/reset-password?token=copy-me"),
    ).toBeInTheDocument();
  });
});

describe("Member detail: delete user", () => {
  beforeEach(() => {
    mintResetMutate.mockReset();
    deleteMutate.mockReset();
    navigate.mockReset();
    toastSuccess.mockReset();
    toastError.mockReset();
  });

  it("asks for confirmation before deleting", async () => {
    renderForm();

    fireEvent.click(screen.getByRole("button", { name: /delete user/i }));

    expect(await screen.findByText(/delete this user\?/i)).toBeInTheDocument();
    expect(deleteMutate).not.toHaveBeenCalled();
  });

  it("deletes and navigates back to the users list on confirm", async () => {
    renderForm();

    fireEvent.click(screen.getByRole("button", { name: /delete user/i }));
    fireEvent.click(await screen.findByRole("button", { name: /^delete$/i }));

    await waitFor(() => expect(deleteMutate).toHaveBeenCalledWith("m-1", expect.anything()));

    const onSuccess = deleteMutate.mock.calls[0][1].onSuccess as () => void;
    onSuccess();
    await waitFor(() => expect(navigate).toHaveBeenCalledWith({ to: "/members" }));
    expect(toastSuccess).toHaveBeenCalled();
  });

  it("does not offer delete when the admin is looking at themselves", () => {
    renderForm({
      id: "m-admin",
      subject: "admin@example.com",
      displayName: "Admin Person",
    });

    expect(screen.queryByRole("button", { name: /delete user/i })).not.toBeInTheDocument();
  });
});
