// With an external OIDC provider the token's `sub` is often a uuid, and the
// member page used to label that uuid "Sign-in email" with no way to change
// it. The page now shows the address the provider reports, the subject only
// as a muted "User ID", and -- when the deployment names the provider's
// account page -- a link there for the person themselves.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const UUID = "5f0c2a8e-3b1d-4c7e-9a64-2d8f1e0b7c35";
const ACCOUNT_URL = "https://id.example.test/realms/acme/account";

type AuthConfig = {
  mode: string;
  memberInvites?: string;
  accountManagementUrl?: string | null;
};

const { authConfigMock, meMock, langRef } = vi.hoisted(() => ({
  authConfigMock: vi.fn((): AuthConfig => ({ mode: "community", memberInvites: "external" })),
  meMock: vi.fn(
    () => ({ subject: "someone-else", memberId: "m-admin" }) as Record<string, unknown>,
  ),
  langRef: { current: "en" as "en" | "de" },
}));

vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return {
    ...actual,
    Link: ({ children, className }: { children?: ReactNode; className?: string }) => (
      <a className={className}>{children}</a>
    ),
    useNavigate: () => vi.fn(),
  };
});

vi.mock("@/lib/i18n", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/i18n")>();
  return {
    ...actual,
    useT: () => (en: string, de?: string) => (langRef.current === "de" ? (de ?? en) : en),
  };
});

vi.mock("@/lib/roles-hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/roles-hooks")>();
  const idle = () => ({ mutate: vi.fn(), isPending: false });
  return {
    ...actual,
    useRoles: () => ({ data: [] }),
    useCreateMember: idle,
    useRenameMemberSubject: idle,
    useSetMemberPassword: idle,
    useAssignRole: idle,
    useGrantSeat: idle,
    useRevokeSeat: idle,
    useMintMemberPasswordReset: idle,
    useDeleteMember: idle,
  };
});

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useAuthConfig: () => ({ data: authConfigMock() }),
    useDepartments: () => ({ data: { items: [] } }),
    useAuth: () => ({ data: meMock() }),
  };
});

import { MemberDetailForm } from "@/routes/members.$memberId";
import { AccountPanel } from "@/routes/profile";

const OIDC_MEMBER = {
  id: "m-1",
  subject: UUID,
  email: "ana@acme.test",
  displayName: "Ana",
};

function wrap(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>);
}

beforeEach(() => {
  langRef.current = "en";
  authConfigMock.mockReset();
  authConfigMock.mockReturnValue({ mode: "community", memberInvites: "external" });
  meMock.mockReset();
  meMock.mockReturnValue({ subject: "someone-else", memberId: "m-admin" });
});

describe("Member detail: sign-in email", () => {
  it("shows the email, not the uuid, as the sign-in email", () => {
    wrap(<MemberDetailForm member={OIDC_MEMBER} />);

    expect(screen.getByDisplayValue("ana@acme.test")).toBeInTheDocument();
    expect(screen.queryByDisplayValue(UUID)).toBeNull();
  });

  it("shows the uuid only as a secondary user id", () => {
    wrap(<MemberDetailForm member={OIDC_MEMBER} />);

    expect(screen.getByText(/user id/i)).toBeInTheDocument();
    expect(screen.getByTestId("user-id")).toHaveTextContent(UUID);
    expect(screen.getByRole("button", { name: /copy user id/i })).toBeInTheDocument();
  });

  it("falls back to the subject when the provider never sent an email", () => {
    wrap(<MemberDetailForm member={{ ...OIDC_MEMBER, email: null }} />);

    expect(screen.getByDisplayValue(UUID)).toBeInTheDocument();
    expect(screen.queryByTestId("user-id")).toBeNull();
  });

  it("keeps the editable field for local sign-in", () => {
    authConfigMock.mockReturnValue({ mode: "community" });
    wrap(
      <MemberDetailForm
        member={{ id: "m-2", subject: "lea@acme.test", email: "lea@acme.test", displayName: "" }}
      />,
    );

    const field = screen.getByDisplayValue("lea@acme.test");
    expect(field).not.toBeDisabled();
    expect(screen.queryByTestId("user-id")).toBeNull();
    expect(screen.queryByRole("link", { name: /change email or password/i })).toBeNull();
  });
});

describe("Member detail: account page link", () => {
  it("keeps the plain hint when no account page is configured", () => {
    meMock.mockReturnValue({ subject: UUID, memberId: "m-1" });
    wrap(<MemberDetailForm member={OIDC_MEMBER} />);

    expect(screen.getByText(/managed by your identity provider/i)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /change email or password/i })).toBeNull();
  });

  it("links the person to their own account page when it is configured", () => {
    authConfigMock.mockReturnValue({
      mode: "community",
      memberInvites: "external",
      accountManagementUrl: ACCOUNT_URL,
    });
    meMock.mockReturnValue({ subject: UUID, memberId: "m-1" });
    wrap(<MemberDetailForm member={OIDC_MEMBER} />);

    const link = screen.getByRole("link", { name: /change email or password/i });
    expect(link).toHaveAttribute("href", ACCOUNT_URL);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
    expect(screen.queryByText(/not editable here/i)).toBeNull();
  });

  it("tells an administrator that each person changes it themselves", () => {
    authConfigMock.mockReturnValue({
      mode: "community",
      memberInvites: "external",
      accountManagementUrl: ACCOUNT_URL,
    });
    wrap(<MemberDetailForm member={OIDC_MEMBER} />);

    expect(screen.queryByRole("link", { name: /change email or password/i })).toBeNull();
    expect(screen.getByText(/each person changes their email/i)).toBeInTheDocument();
  });

  it("speaks German with formal address", () => {
    langRef.current = "de";
    authConfigMock.mockReturnValue({
      mode: "community",
      memberInvites: "external",
      accountManagementUrl: ACCOUNT_URL,
    });
    meMock.mockReturnValue({ subject: UUID, memberId: "m-1" });
    wrap(<MemberDetailForm member={OIDC_MEMBER} />);

    expect(screen.getByText("Anmelde-E-Mail")).toBeInTheDocument();
    expect(screen.getByText(/Benutzer-ID/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "E-Mail oder Passwort ändern" })).toBeInTheDocument();
  });

  it("speaks German to an administrator looking at someone else", () => {
    langRef.current = "de";
    authConfigMock.mockReturnValue({
      mode: "community",
      memberInvites: "external",
      accountManagementUrl: ACCOUNT_URL,
    });
    wrap(<MemberDetailForm member={OIDC_MEMBER} />);

    expect(
      screen.getByText(
        "Jede Person ändert ihre E-Mail-Adresse und ihr Passwort selbst in ihrem eigenen Konto.",
      ),
    ).toBeInTheDocument();
  });
});

describe("Profile: account panel", () => {
  it("offers the account page instead of local forms when configured", () => {
    authConfigMock.mockReturnValue({
      mode: "community",
      memberInvites: "external",
      accountManagementUrl: ACCOUNT_URL,
    });
    meMock.mockReturnValue({ subject: UUID, email: "ana@acme.test", memberId: "m-1" });
    const { container } = wrap(<AccountPanel initialDisplayName="Ana" />);

    const panel = within(container);
    expect(panel.getByText("ana@acme.test")).toBeInTheDocument();
    expect(panel.getByTestId("user-id")).toHaveTextContent(UUID);
    expect(panel.getByRole("link", { name: /change email or password/i })).toHaveAttribute(
      "href",
      ACCOUNT_URL,
    );
    expect(panel.queryByRole("button", { name: /^change password$/i })).toBeNull();
    expect(panel.queryByRole("button", { name: /^change email$/i })).toBeNull();
  });

  it("keeps the existing forms when no account page is configured", () => {
    meMock.mockReturnValue({ subject: UUID, email: "ana@acme.test", memberId: "m-1" });
    wrap(<AccountPanel initialDisplayName="Ana" />);

    expect(screen.queryByRole("link", { name: /change email or password/i })).toBeNull();
    expect(screen.getByRole("button", { name: /^change password$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^change email$/i })).toBeInTheDocument();
  });
});
