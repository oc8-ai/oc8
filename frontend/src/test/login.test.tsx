import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { RouterProvider } from "@tanstack/react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { getRouter } from "../router";
import { PublicAuthLayout } from "../components/public-auth-layout";
import { LoginPage } from "../routes/login";

const {
  navigate,
  toastError,
  toastWarning,
  enrollTotp,
  confirmTotp,
  verifyTotp,
  renderTotpQrDataUrl,
  sendEmailCode,
} = vi.hoisted(() => ({
  sendEmailCode: vi.fn(),
  navigate: vi.fn(),
  toastError: vi.fn(),
  toastWarning: vi.fn(),
  enrollTotp: vi.fn(),
  confirmTotp: vi.fn(),
  verifyTotp: vi.fn(),
  renderTotpQrDataUrl: vi.fn(),
}));

vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return { ...actual, useNavigate: () => navigate };
});

vi.mock("../components/dev-sign-in", () => ({
  DevSignIn: () => <div>Dev sign-in path</div>,
}));

vi.mock("@/lib/i18n", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/i18n")>();
  return { ...actual, useT: () => (en: string) => en };
});
vi.mock("sonner", async (importOriginal) => {
  const actual = await importOriginal<typeof import("sonner")>();
  return {
    ...actual,
    toast: {
      ...actual.toast,
      error: toastError,
      warning: toastWarning,
      info: vi.fn(),
      success: vi.fn(),
    },
  };
});

// The TOTP components (Task 11) talk to the three shared endpoints through
// `@/lib/totp`; stub the client the same way `totp-challenge.test.tsx` and
// `totp-enroll.test.tsx` do, so these route-level tests exercise the wiring
// rather than the QR renderer or a second fetch sequence.
vi.mock("@/lib/totp", () => ({
  enrollTotp,
  confirmTotp,
  verifyTotp,
  renderTotpQrDataUrl,
  sendEmailCode,
  verifyEmailCode: vi.fn(),
  enrollEmailCode: vi.fn(),
  confirmEmailCode: vi.fn(),
}));

describe("Community login route", () => {
  beforeEach(() => {
    navigate.mockReset();
    toastError.mockReset();
    toastWarning.mockReset();
    enrollTotp.mockReset();
    enrollTotp.mockResolvedValue({ secret: "ABCDEFGH", provisioningUri: "otpauth://totp/x" });
    confirmTotp.mockReset();
    confirmTotp.mockResolvedValue({ backupCodes: ["CODE1", "CODE2"] });
    verifyTotp.mockReset();
    verifyTotp.mockResolvedValue({
      token: "real-session-token",
      principal: {},
      memberId: "m1",
    });
    renderTotpQrDataUrl.mockReset();
    renderTotpQrDataUrl.mockResolvedValue("data:image/png;base64,fake");
    // Tokens outlive a test otherwise -- a later assertion would then read the
    // token a previous test's successful login stored.
    window.localStorage.removeItem("oc8-community-token");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => ({ mode: "local" }) }),
    );
  });

  it("renders the docking station frame and decorative companion, with no logo above the card", () => {
    render(
      <PublicAuthLayout mood="idle">
        <p>Authentication form</p>
      </PublicAuthLayout>,
    );

    // The only oc8 brand mark lives inside the login/setup card itself (see the
    // login route's own tests) -- PublicAuthLayout no longer renders one above it.
    expect(screen.queryByRole("img", { name: "oc8" })).not.toBeInTheDocument();
    expect(screen.getByAltText("")).toHaveAttribute("src", "/octopus_oc8.svg");
    expect(screen.getByTestId("octopus-companion")).toHaveAttribute("data-mood", "idle");
  });

  it("shows the local first-admin setup UI after Community auth config loads", async () => {
    render(<LoginPage />);

    expect(
      await screen.findByRole("heading", { name: "Create Admin Account" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
    expect(screen.getByLabelText("Display Name")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create Account" })).toBeDisabled();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("shows the login form, not the setup form, once an admin already exists", async () => {
    // The bug: /auth/config always produced the Create-Admin form, so an
    // operator whose session expired mid-wizard was asked to create an
    // account that already existed and only found out from the 422.
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ mode: "community", initialized: true }),
      }),
    );

    render(<LoginPage />);

    expect(await screen.findByRole("button", { name: "Sign In" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Create Admin Account" })).toBeNull();
    expect(screen.queryByLabelText("Display Name")).toBeNull();
  });

  it("still offers setup on an instance that has no admin yet", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ mode: "community", initialized: false }),
      }),
    );

    render(<LoginPage />);

    expect(
      await screen.findByRole("heading", { name: "Create Admin Account" }),
    ).toBeInTheDocument();
  });

  it("renders the Community login route outside the application shell", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => ({ mode: "community" }) }),
    );
    window.history.replaceState({}, "", "/login");
    const router = getRouter();
    await router.load();

    render(<RouterProvider router={router} />);

    expect(await screen.findByRole("heading", { name: "Create Admin Account" })).toBeVisible();
    expect(screen.queryByText("Office")).not.toBeInTheDocument();
    expect(screen.queryByText("Settings")).not.toBeInTheDocument();
  });

  // Testing Library's `render` wraps in `act`, which flushes effects before the
  // first assertion can run -- so it cannot observe the pre-hydration paint.
  // Static server markup can: `hydrated` is false there, exactly as it is on the
  // real first paint (SSR HTML and the initial client render).
  async function renderRouteToStaticMarkup(pathname: string) {
    window.history.replaceState({}, "", pathname);
    const router = getRouter();
    await router.load();
    const { renderToStaticMarkup } = await import("react-dom/server");
    const html = renderToStaticMarkup(<RouterProvider router={router} />);
    // Only the body: <head> legitimately carries the "AI Agent Control Panel" title.
    return html.slice(html.indexOf("<body>"));
  }

  it("never renders the application shell on the Community login route, not even before hydration", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => ({ mode: "community" }) }),
    );

    const body = await renderRouteToStaticMarkup("/login");

    expect(body).toContain("oc8 docking station");
    // Dashboard header + sidebar markers from `AppShell`.
    expect(body).not.toContain("Control Panel");
    expect(body).not.toContain("Office");
    expect(body).not.toContain("App Store");
    expect(body).not.toContain("All systems operational");
  });

  it("keeps the application shell away from /login for a stale session", async () => {
    window.localStorage.setItem("oc8-community-token", "stale-token");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => ({ mode: "community" }) }),
    );

    try {
      const body = await renderRouteToStaticMarkup("/login");
      expect(body).toContain("oc8 docking station");
      expect(body).not.toContain("Control Panel");

      const router = getRouter();
      await router.load();
      render(<RouterProvider router={router} />);

      expect(await screen.findByRole("heading", { name: "Create Admin Account" })).toBeVisible();
      expect(screen.queryByText("Office")).not.toBeInTheDocument();
      expect(screen.queryByText("Control Panel")).not.toBeInTheDocument();
    } finally {
      window.localStorage.removeItem("oc8-community-token");
    }
  });

  // The Toaster is no longer mounted by `PublicAuthLayout` -- it is a single
  // app-wide one in `routes/__root.tsx`, above the route swap, because a
  // per-layout Toaster swallowed every toast raised right before navigating
  // away from /login (see root-toaster.test.tsx). What still has to hold is
  // that toasts raised from the login route are visible at all, so render the
  // route the way the app does: through the router, root included.
  it("mounts a toaster on the public login route so auth feedback is visible", async () => {
    const sonner = await vi.importActual<typeof import("sonner")>("sonner");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ mode: "community", initialized: false }),
      }),
    );
    window.history.replaceState({}, "", "/login");
    const router = getRouter();
    await router.load();

    render(<RouterProvider router={router} />);
    await screen.findByRole("heading", { name: "Create Admin Account" });

    act(() => {
      sonner.toast.error("Docking station toast");
    });

    expect(await screen.findByText("Docking station toast")).toBeInTheDocument();
  });

  it("does not mount a second Toaster inside the public layout", async () => {
    render(
      <PublicAuthLayout mood="idle">
        <p>Authentication form</p>
      </PublicAuthLayout>,
    );

    // Two Toasters would mean toasts render twice on /login once the root one
    // is in place -- and a per-layout one is exactly what the route-swap bug
    // was made of.
    expect(document.querySelector("[aria-label^='Notifications']")).toBeNull();
  });

  it("keeps the polite live region mounted before any notice fires", async () => {
    render(<LoginPage />);
    await screen.findByRole("heading", { name: "Create Admin Account" });

    const liveRegion = screen.getByTestId("auth-notice");
    expect(liveRegion).toHaveAttribute("aria-live", "polite");
    expect(liveRegion).toHaveTextContent("");
  });

  it("shows the dev sign-in pathway for a direct login visit", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => ({ mode: "dev" }) }),
    );
    window.history.replaceState({}, "", "/login");
    const router = getRouter();
    await router.load();

    render(<RouterProvider router={router} />);

    expect(await screen.findByText("Dev sign-in path")).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Create Admin Account" })).not.toBeInTheDocument();
  });

  it("rejects mismatched setup passwords locally without a setup request", async () => {
    const fetchMock = vi.mocked(fetch);
    render(<LoginPage />);
    await screen.findByRole("heading", { name: "Create Admin Account" });

    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "admin@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "password-one" } });
    fireEvent.change(screen.getByLabelText("Confirm Password"), {
      target: { value: "password-two" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create Account" }));

    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Passwords do not match"));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("shows a thinking companion mood while a setup submission is in flight", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce({ ok: true, json: async () => ({ mode: "community" }) })
        .mockReturnValueOnce(new Promise(() => {})),
    );

    render(<LoginPage />);
    await screen.findByRole("heading", { name: "Create Admin Account" });
    expect(screen.getByTestId("octopus-companion")).toHaveAttribute("data-mood", "idle");

    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "admin@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "password-one" } });
    fireEvent.change(screen.getByLabelText("Confirm Password"), {
      target: { value: "password-one" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create Account" }));

    await waitFor(() =>
      expect(screen.getByTestId("octopus-companion")).toHaveAttribute("data-mood", "thinking"),
    );
  });

  it("switches to the login form with a live notice when setup reports the instance is already initialized", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce({ ok: true, json: async () => ({ mode: "community" }) })
        .mockResolvedValueOnce({
          ok: false,
          status: 422,
          json: async () => ({ detail: "Instance already initialized" }),
        }),
    );

    render(<LoginPage />);
    await screen.findByRole("heading", { name: "Create Admin Account" });

    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "admin@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "password-one" } });
    fireEvent.change(screen.getByLabelText("Confirm Password"), {
      target: { value: "password-one" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create Account" }));

    expect(await screen.findByRole("heading", { name: "Sign In" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Display Name")).not.toBeInTheDocument();

    const notice = screen.getByText("Instance already initialized. Please log in.");
    expect(notice).toHaveAttribute("aria-live", "polite");
  });

  it("flashes a celebrating companion mood and navigates immediately after a successful setup", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce({ ok: true, json: async () => ({ mode: "community" }) })
        .mockResolvedValueOnce({ ok: true, json: async () => ({ token: "test-token" }) }),
    );

    render(<LoginPage />);
    await screen.findByRole("heading", { name: "Create Admin Account" });

    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "admin@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "password-one" } });
    fireEvent.change(screen.getByLabelText("Confirm Password"), {
      target: { value: "password-one" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create Account" }));

    await waitFor(() =>
      expect(screen.getByTestId("octopus-companion")).toHaveAttribute("data-mood", "celebrating"),
    );
    expect(navigate).toHaveBeenCalledWith({ to: "/welcome" });
    expect(window.localStorage.getItem("oc8-community-token")).toBe("test-token");
  });

  // --- Two-factor outcomes of POST /auth/login -------------------------------

  // `/auth/config` first (the mount-time check), then the login POST itself.
  function stubConfigThenLogin(loginBody: Record<string, unknown>) {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce({
          ok: true,
          json: async () => ({ mode: "community", initialized: true }),
        })
        .mockResolvedValueOnce({ ok: true, json: async () => loginBody }),
    );
  }

  async function submitLoginForm() {
    await screen.findByRole("button", { name: "Sign In" });
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "admin@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "password-one" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign In" }));
  }

  it("shows the TOTP challenge screen when login response says requiresTotpCode", async () => {
    stubConfigThenLogin({ requiresTotpCode: true, token: "challenge-tok" });

    render(<LoginPage />);
    await submitLoginForm();

    expect(
      await screen.findByRole("heading", { name: /authentication code/i }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign In" })).not.toBeInTheDocument();
    // A narrow challenge token is not a session: it must not be stored, and
    // login must not be treated as complete.
    expect(window.localStorage.getItem("oc8-community-token")).toBeNull();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("shows the enrollment screen when login response says requiresTotpEnrollment", async () => {
    stubConfigThenLogin({ requiresTotpEnrollment: true, token: "enroll-tok" });

    render(<LoginPage />);
    await submitLoginForm();

    expect(
      await screen.findByRole("heading", { name: /scan with your authenticator/i }),
    ).toBeInTheDocument();
    expect(enrollTotp).toHaveBeenCalledWith("enroll-tok");
    expect(window.localStorage.getItem("oc8-community-token")).toBeNull();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("offers the methods the login response names on the challenge", async () => {
    stubConfigThenLogin({
      requiresTotpCode: true,
      token: "challenge-tok",
      twoFactorMethods: ["totp", "email"],
    });

    render(<LoginPage />);
    await submitLoginForm();

    fireEvent.click(await screen.findByRole("button", { name: "Code by email" }));
    expect(screen.getByRole("button", { name: "Send code" })).toBeInTheDocument();
    expect(window.localStorage.getItem("oc8-community-token")).toBeNull();
  });

  it("lets a member choose which method to enrol in", async () => {
    stubConfigThenLogin({
      requiresTotpEnrollment: true,
      token: "enroll-tok",
      twoFactorMethods: ["totp", "email"],
    });

    render(<LoginPage />);
    await submitLoginForm();

    fireEvent.click(await screen.findByRole("button", { name: "Code by email" }));
    expect(await screen.findByRole("heading", { name: /codes by email/i })).toBeInTheDocument();
    expect(enrollTotp).not.toHaveBeenCalled();
  });

  it("goes back to the page the session ended on, query and all", async () => {
    window.sessionStorage.setItem("oc8-return-to", "/agents/a1?tab=config");
    stubConfigThenLogin({ token: "session-tok" });

    render(<LoginPage />);
    await submitLoginForm();

    await waitFor(() => expect(navigate).toHaveBeenCalledWith({ href: "/agents/a1?tab=config" }));
  });

  it("shows a grace-period nag when totpGraceExpiresAt is present, and still navigates", async () => {
    const expiresAt = new Date(Date.now() + 7 * 24 * 60 * 60 * 1000).toISOString();
    stubConfigThenLogin({ token: "session-tok", totpGraceExpiresAt: expiresAt });

    render(<LoginPage />);
    await submitLoginForm();

    // The nag warns; it does not block. (Blocking is the requiresTotpEnrollment
    // branch above, which is what the grace period escalates to once expired.)
    await waitFor(() =>
      expect(toastWarning).toHaveBeenCalledWith(
        expect.stringContaining("within 7 day(s)"),
        expect.objectContaining({ duration: 10000 }),
      ),
    );
    expect(navigate).toHaveBeenCalledWith({ to: "/" });
    expect(window.localStorage.getItem("oc8-community-token")).toBe("session-tok");
  });

  it("never nags about 0 or fewer days when the client clock runs ahead of the deadline", async () => {
    // The backend only ever hands back a future deadline, so this is pure
    // clock skew -- but "within 0 day(s)" (or a negative number) reads as a
    // bug rather than as urgency. Floor at one day.
    const expiresAt = new Date(Date.now() - 60 * 60 * 1000).toISOString();
    stubConfigThenLogin({ token: "session-tok", totpGraceExpiresAt: expiresAt });

    render(<LoginPage />);
    await submitLoginForm();

    await waitFor(() =>
      expect(toastWarning).toHaveBeenCalledWith(
        expect.stringContaining("within 1 day(s)"),
        expect.objectContaining({ duration: 10000 }),
      ),
    );
  });

  it("after TotpChallenge verifies, stores the real session token and navigates", async () => {
    stubConfigThenLogin({ requiresTotpCode: true, token: "challenge-tok" });

    render(<LoginPage />);
    await submitLoginForm();
    await screen.findByRole("heading", { name: /authentication code/i });

    fireEvent.change(screen.getByLabelText("Code"), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify" }));

    await waitFor(() => expect(verifyTotp).toHaveBeenCalledWith("challenge-tok", "123456"));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith({ to: "/" }));
    // The session token from /auth/totp/verify, never the narrow challenge one.
    expect(window.localStorage.getItem("oc8-community-token")).toBe("real-session-token");
  });

  it("returns to the login form after enrollment completes, without minting a session", async () => {
    stubConfigThenLogin({ requiresTotpEnrollment: true, token: "enroll-tok" });

    render(<LoginPage />);
    await submitLoginForm();
    await screen.findByRole("heading", { name: /scan with your authenticator/i });

    fireEvent.change(screen.getByLabelText(/6-digit code/i), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await screen.findByText("CODE1");
    fireEvent.click(screen.getByRole("button", { name: /saved/i }));

    // Confirming enrollment does not log anyone in -- back to the form.
    expect(await screen.findByRole("button", { name: "Sign In" })).toBeInTheDocument();
    expect(window.localStorage.getItem("oc8-community-token")).toBeNull();
    expect(navigate).not.toHaveBeenCalled();
  });
});
