import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// vi.hoisted: `vi.mock` factories below are hoisted above ordinary `const`s
// by vitest, so a plain top-level `const apiGetMock = vi.fn()` referenced
// inside the factory throws "Cannot access before initialization" -- see
// the identical pattern in `hooks.test.tsx`/`credential-hooks.test.tsx`.
const {
  removeSecondFactorMock,
  passkeyRegistrationOptionsMock,
  verifyPasskeyRegistrationMock,
  renamePasskeyMock,
  removePasskeyMock,
} = vi.hoisted(() => ({
  removeSecondFactorMock: vi.fn(),
  passkeyRegistrationOptionsMock: vi.fn(),
  verifyPasskeyRegistrationMock: vi.fn(),
  renamePasskeyMock: vi.fn(),
  removePasskeyMock: vi.fn(),
}));

const {
  apiGetMock,
  apiPostMock,
  apiPutMock,
  hasCommunitySessionMock,
  logoutCommunityMock,
  logoutDevMock,
} = vi.hoisted(() => ({
  apiGetMock: vi.fn(),
  apiPostMock: vi.fn(),
  apiPutMock: vi.fn(),
  hasCommunitySessionMock: vi.fn().mockReturnValue(false),
  logoutCommunityMock: vi.fn(),
  logoutDevMock: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, get: apiGetMock, post: apiPostMock, put: apiPutMock },
    getToken: vi.fn().mockResolvedValue("fake-token"),
    hasCommunitySession: hasCommunitySessionMock,
    logoutCommunity: logoutCommunityMock,
    logoutDev: logoutDevMock,
  };
});
vi.mock("@/lib/push-notifications", () => ({
  isPushSupported: () => false,
  getPushSubscriptionStatus: vi.fn().mockResolvedValue(null),
  subscribeToPush: vi.fn(),
  unsubscribeFromPush: vi.fn(),
}));
// <TotpEnroll> (Task 11) talks to the three shared TOTP endpoints via
// `@/lib/totp`'s raw fetch client, not `@/lib/api` -- mocked the same way
// `totp-enroll.test.tsx` already mocks it, so mounting <TotpEnroll> here
// doesn't depend on a real backend being reachable.
vi.mock("@/lib/totp", () => ({
  enrollTotp: vi.fn(async () => ({ secret: "ABCDEFGH", provisioningUri: "otpauth://totp/x" })),
  confirmTotp: vi.fn(async () => ({ backupCodes: ["CODE1", "CODE2"] })),
  renderTotpQrDataUrl: vi.fn(async () => "data:image/png;base64,fake"),
  enrollEmailCode: vi.fn(async () => ({ sentTo: "ada@example.com" })),
  confirmEmailCode: vi.fn(async () => ({ backupCodes: [] })),
  removeSecondFactor: removeSecondFactorMock,
  passkeyRegistrationOptions: passkeyRegistrationOptionsMock,
  verifyPasskeyRegistration: verifyPasskeyRegistrationMock,
  renamePasskey: renamePasskeyMock,
  removePasskey: removePasskeyMock,
}));
// ApprovalChannelsPanel's own hooks -- mocked the same way the panel above
// mocks TOTP/push, so this file's real target (the TOTP card) doesn't have
// to route unrelated network calls through the blanket `apiGetMock`, which
// answers every GET the same way and would otherwise resolve `useAvailableChannels`
// to a non-array value.
vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useAvailableChannels: () => ({ data: [] }),
    useChannelBindings: () => ({ data: [] }),
    useRequestChannelLink: () => ({ mutate: vi.fn(), isPending: false }),
    useRevokeChannelBinding: () => ({ mutate: vi.fn(), isPending: false }),
  };
});
vi.mock("@/lib/governance-hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/governance-hooks")>();
  return { ...actual, useCan: () => () => false };
});
// Only `toast.error`/`toast.success` are overridden -- everything else
// (Toaster, etc.) passes through untouched, and this file's original two
// tests never asserted on a toast, so this addition doesn't change them.
const { toastErrorMock, toastSuccessMock } = vi.hoisted(() => ({
  toastErrorMock: vi.fn(),
  toastSuccessMock: vi.fn(),
}));
vi.mock("sonner", async (importOriginal) => {
  const actual = await importOriginal<typeof import("sonner")>();
  return {
    ...actual,
    toast: { ...actual.toast, error: toastErrorMock, success: toastSuccessMock },
  };
});

import { ProfilePage } from "@/routes/profile";

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ProfilePage />
    </QueryClientProvider>,
  );
}

describe("Profile page — two-factor card", () => {
  const NOTHING = {
    methods: [],
    allowedMethods: ["totp", "email", "passkey"],
    enrollableMethods: ["totp", "email"],
    passkeys: [],
    required: false,
    backupCodesLeft: 0,
  };
  const BOTH = {
    methods: [
      { method: "totp", label: "", allowed: true, removable: true },
      { method: "email", label: "ada@example.com", allowed: true, removable: true },
    ],
    allowedMethods: ["totp", "email", "passkey"],
    enrollableMethods: ["totp", "email"],
    passkeys: [],
    required: true,
    backupCodesLeft: 8,
  };
  const PASSKEYS = {
    methods: [{ method: "passkey", label: "", allowed: true, removable: true }],
    passkeys: [
      {
        id: "pk-1",
        label: "Laptop",
        createdAt: "2026-11-20T10:00:00Z",
        lastUsedAt: null,
        removable: true,
      },
      {
        id: "pk-2",
        label: "Security key",
        createdAt: "2026-11-20T11:00:00Z",
        lastUsedAt: "2026-11-20T12:00:00Z",
        removable: true,
      },
    ],
    allowedMethods: ["totp", "email", "passkey"],
    enrollableMethods: ["totp", "email", "passkey"],
    required: true,
    backupCodesLeft: 10,
  };

  function twoFactor(body: unknown) {
    apiGetMock.mockImplementation((path: string) =>
      path === "/auth/two-factor/methods" ? Promise.resolve(body) : Promise.resolve({}),
    );
  }

  beforeEach(() => {
    apiGetMock.mockReset();
    apiPostMock.mockReset();
    removeSecondFactorMock.mockReset();
    passkeyRegistrationOptionsMock.mockReset();
    verifyPasskeyRegistrationMock.mockReset();
    renamePasskeyMock.mockReset();
    removePasskeyMock.mockReset();
    toastErrorMock.mockReset();
    toastSuccessMock.mockReset();
  });

  it("offers the enrollable methods and starts authenticator enrollment", async () => {
    twoFactor(NOTHING);
    renderPage();
    await waitFor(() => expect(screen.getByText(/not enabled/i)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /add code by email/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /add authenticator app/i }));
    // TotpEnroll (Task 11) is now mounted -- assert on its own visible text
    // (e.g. a heading it renders), not implementation details.
    await waitFor(() =>
      expect(screen.getByText(/scan with your authenticator app/i)).toBeInTheDocument(),
    );
  });

  it("shows a regenerate button when enrolled, and displays fresh codes", async () => {
    twoFactor(BOTH);
    apiPostMock.mockResolvedValue({ backupCodes: ["AAAA-1111", "BBBB-2222"] });
    renderPage();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /regenerate/i })).toBeInTheDocument(),
    );
    expect(screen.getByText(/8 backup codes left/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /regenerate/i }));
    await waitFor(() => expect(screen.getByText("AAAA-1111")).toBeInTheDocument());
  });

  it("removes a method after asking for the password", async () => {
    twoFactor(BOTH);
    removeSecondFactorMock.mockResolvedValue(undefined);
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /remove code by email/i }));
    fireEvent.change(screen.getByPlaceholderText(/current password/i), {
      target: { value: "hunter22" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^remove$/i }));

    await waitFor(() =>
      expect(removeSecondFactorMock).toHaveBeenCalledWith("fake-token", "email", "hunter22"),
    );
    await waitFor(() => expect(toastSuccessMock).toHaveBeenCalled());
  });

  it("shows the refusal when the method cannot be removed", async () => {
    twoFactor(BOTH);
    removeSecondFactorMock.mockRejectedValue(new Error("current password is incorrect"));
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /remove authenticator app/i }));
    fireEvent.change(screen.getByPlaceholderText(/current password/i), {
      target: { value: "wrong" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^remove$/i }));

    await waitFor(() =>
      expect(toastErrorMock).toHaveBeenCalledWith(
        expect.any(String),
        expect.objectContaining({ description: "current password is incorrect" }),
      ),
    );
  });

  it("does not offer removing the last method the policy requires", async () => {
    twoFactor({
      ...BOTH,
      methods: [{ method: "totp", label: "", allowed: true, removable: false }],
    });
    renderPage();

    expect(await screen.findByRole("button", { name: /remove authenticator app/i })).toBeDisabled();
  });

  it("lists each passkey by its name", async () => {
    twoFactor(PASSKEYS);
    renderPage();

    expect(await screen.findByText("Laptop")).toBeInTheDocument();
    expect(screen.getByText("Security key")).toBeInTheDocument();
    // Passkeys are removed one by one, not as a whole method.
    expect(screen.queryByRole("button", { name: /^remove passkey or security key$/i })).toBeNull();
  });

  it("adds a passkey with a name", async () => {
    twoFactor({ ...PASSKEYS, passkeys: [], methods: [] });
    passkeyRegistrationOptionsMock.mockResolvedValue({
      options: {
        rp: { id: "localhost", name: "oc8" },
        user: { id: "AQID", name: "ada@example.com", displayName: "Ada" },
        challenge: "AAEC",
        pubKeyCredParams: [{ type: "public-key", alg: -7 }],
        excludeCredentials: [],
      },
    });
    verifyPasskeyRegistrationMock.mockResolvedValue({ id: "pk-3", backupCodes: [] });
    const create = vi.fn().mockResolvedValue(fakeAttestation());
    Object.defineProperty(navigator, "credentials", {
      value: { create, get: vi.fn() },
      configurable: true,
    });
    Object.defineProperty(window, "PublicKeyCredential", {
      value: function PublicKeyCredential() {},
      configurable: true,
    });
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /add passkey/i }));
    fireEvent.change(await screen.findByLabelText(/name this passkey/i), {
      target: { value: "Work laptop" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^create passkey$/i }));

    await waitFor(() =>
      expect(verifyPasskeyRegistrationMock).toHaveBeenCalledWith(
        "fake-token",
        expect.objectContaining({ id: "cred-1", type: "public-key" }),
        "Work laptop",
      ),
    );
    expect(create).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(toastSuccessMock).toHaveBeenCalled());
  });

  it("renames a passkey", async () => {
    twoFactor(PASSKEYS);
    renamePasskeyMock.mockResolvedValue(undefined);
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /rename laptop/i }));
    const input = screen.getByLabelText(/new name for laptop/i);
    fireEvent.change(input, { target: { value: "Home laptop" } });
    fireEvent.click(within(input.closest("li")!).getByRole("button", { name: /^save$/i }));

    await waitFor(() =>
      expect(renamePasskeyMock).toHaveBeenCalledWith("fake-token", "pk-1", "Home laptop"),
    );
  });

  it("removes one passkey after asking for the password", async () => {
    twoFactor(PASSKEYS);
    removePasskeyMock.mockResolvedValue(undefined);
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /remove security key/i }));
    fireEvent.change(screen.getByPlaceholderText(/current password/i), {
      target: { value: "hunter22" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^remove$/i }));

    await waitFor(() =>
      expect(removePasskeyMock).toHaveBeenCalledWith("fake-token", "pk-2", "hunter22"),
    );
    expect(removeSecondFactorMock).not.toHaveBeenCalled();
  });

  it("does not offer removing the last passkey the policy requires", async () => {
    twoFactor({
      ...PASSKEYS,
      passkeys: [{ ...PASSKEYS.passkeys[0], removable: false }],
    });
    renderPage();

    expect(await screen.findByRole("button", { name: /remove laptop/i })).toBeDisabled();
  });
});

function buffer(bytes: number[]): ArrayBuffer {
  return new Uint8Array(bytes).buffer;
}

/** What `navigator.credentials.create()` resolves to, as far as the page reads it. */
function fakeAttestation() {
  return {
    id: "cred-1",
    rawId: buffer([1, 2, 3]),
    type: "public-key",
    authenticatorAttachment: "platform",
    response: {
      clientDataJSON: buffer([4, 5]),
      attestationObject: buffer([6, 7]),
      getTransports: () => ["internal"],
    },
    getClientExtensionResults: () => ({}),
  };
}

describe("Profile page — Account panel", () => {
  const currentUser = { displayName: "Ada Lovelace", subject: "ada@example.com", role: "member" };
  const reloadMock = vi.fn();

  beforeEach(() => {
    apiGetMock.mockReset();
    apiPostMock.mockReset();
    apiPutMock.mockReset();
    hasCommunitySessionMock.mockReset().mockReturnValue(false);
    logoutCommunityMock.mockReset();
    logoutDevMock.mockReset();
    toastErrorMock.mockReset();
    toastSuccessMock.mockReset();
    // Path-aware, since AccountPanel's `useAuth()` (`GET /me`) and the TOTP
    // card's `GET /auth/two-factor/methods` share this one mock.
    apiGetMock.mockImplementation((path: string) => {
      if (path === "/me") return Promise.resolve(currentUser);
      if (path === "/auth/two-factor/methods") return Promise.resolve({});
      return Promise.resolve({});
    });
    // jsdom's `window.location.reload` isn't configurable, so `vi.spyOn`
    // throws "Cannot redefine property" -- replace the whole `location`
    // instead, the same workaround used elsewhere for this jsdom limitation.
    reloadMock.mockReset();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...window.location, reload: reloadMock },
    });
  });

  async function fetchOk(body: unknown) {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => body }),
    );
  }

  async function fetchError(status: number, detail: string) {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status, json: async () => ({ detail }) }),
    );
  }

  it("seeds the display-name field from GET /me and saves an edit via PUT /auth/me/display-name", async () => {
    apiPutMock.mockResolvedValue({ ...currentUser, displayName: "Ada L." });
    renderPage();

    const input = await screen.findByDisplayValue("Ada Lovelace");
    fireEvent.change(input, { target: { value: "Ada L." } });
    // If a form is open, two "Save" buttons exist (display-name's own,
    // always present, plus this form's) -- this form's is the last one.
    fireEvent.click(screen.getAllByRole("button", { name: "Save" }).at(-1)!);

    await waitFor(() =>
      expect(apiPutMock).toHaveBeenCalledWith("/auth/me/display-name", { displayName: "Ada L." }),
    );
    await waitFor(() => expect(toastSuccessMock).toHaveBeenCalled());
  });

  it("shows the current password as the error when changing the password with a wrong one", async () => {
    await fetchError(401, "current password is incorrect");
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /change password/i }));
    fireEvent.change(screen.getByPlaceholderText("Current password"), {
      target: { value: "wrong-pass" },
    });
    fireEvent.change(screen.getByPlaceholderText("New password"), {
      target: { value: "a-new-password" },
    });
    // If a form is open, two "Save" buttons exist (display-name's own,
    // always present, plus this form's) -- this form's is the last one.
    fireEvent.click(screen.getAllByRole("button", { name: "Save" }).at(-1)!);

    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        expect.stringContaining("/auth/me/password"),
        expect.objectContaining({
          method: "PUT",
          headers: expect.objectContaining({ authorization: "Bearer fake-token" }),
        }),
      ),
    );
    await waitFor(() =>
      expect(toastErrorMock).toHaveBeenCalledWith(
        "Could not change your password.",
        expect.objectContaining({ description: "current password is incorrect" }),
      ),
    );
    // A 401 here must NOT trip `@/lib/api`'s shared "session expired" handler
    // -- see `putConfirmingPassword`'s comment in profile.tsx. Nobody got
    // signed out over a typo.
    expect(logoutDevMock).not.toHaveBeenCalled();
    expect(logoutCommunityMock).not.toHaveBeenCalled();
    expect(reloadMock).not.toHaveBeenCalled();
  });

  it("shows a confirmation message when email change requires verification", async () => {
    await fetchOk({ verificationRequired: true, sentTo: "new@example.com", reauthRequired: false });
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /change email/i }));
    fireEvent.change(screen.getByPlaceholderText("Current password"), {
      target: { value: "correct-pass" },
    });
    fireEvent.change(screen.getByPlaceholderText("New email"), {
      target: { value: "new@example.com" },
    });
    // If a form is open, two "Save" buttons exist (display-name's own,
    // always present, plus this form's) -- this form's is the last one.
    fireEvent.click(screen.getAllByRole("button", { name: "Save" }).at(-1)!);

    await waitFor(() =>
      expect(screen.getByText(/check your inbox at new@example\.com/i)).toBeInTheDocument(),
    );
    expect(logoutDevMock).not.toHaveBeenCalled();
    expect(reloadMock).not.toHaveBeenCalled();
  });

  it("shows a 409 conflict message when the new email is already taken", async () => {
    await fetchError(409, "Another user already signs in with that identity.");
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /change email/i }));
    fireEvent.change(screen.getByPlaceholderText("Current password"), {
      target: { value: "correct-pass" },
    });
    fireEvent.change(screen.getByPlaceholderText("New email"), {
      target: { value: "taken@example.com" },
    });
    // If a form is open, two "Save" buttons exist (display-name's own,
    // always present, plus this form's) -- this form's is the last one.
    fireEvent.click(screen.getAllByRole("button", { name: "Save" }).at(-1)!);

    await waitFor(() =>
      expect(toastErrorMock).toHaveBeenCalledWith(
        "Could not change your email.",
        expect.objectContaining({
          description: "Another user already signs in with that identity.",
        }),
      ),
    );
  });

  it("signs the caller out and reloads when the email change reports reauthRequired", async () => {
    await fetchOk({ verificationRequired: false, reauthRequired: true });
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /change email/i }));
    fireEvent.change(screen.getByPlaceholderText("Current password"), {
      target: { value: "correct-pass" },
    });
    fireEvent.change(screen.getByPlaceholderText("New email"), {
      target: { value: "renamed@example.com" },
    });
    // If a form is open, two "Save" buttons exist (display-name's own,
    // always present, plus this form's) -- this form's is the last one.
    fireEvent.click(screen.getAllByRole("button", { name: "Save" }).at(-1)!);

    // reauthRequired: true -- the backend has just rewritten the caller's
    // sign-in identity, so the frontend MUST sign them out rather than only
    // toast about it (see profile.tsx's `changeEmail`).
    await waitFor(() => expect(logoutDevMock).toHaveBeenCalled());
    expect(hasCommunitySessionMock).toHaveBeenCalled();
    expect(logoutCommunityMock).not.toHaveBeenCalled();
    expect(reloadMock).toHaveBeenCalled();
  });

  it("uses logoutCommunity (not logoutDev) when a community session is active", async () => {
    hasCommunitySessionMock.mockReturnValue(true);
    await fetchOk({ verificationRequired: false, reauthRequired: true });
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /change email/i }));
    fireEvent.change(screen.getByPlaceholderText("Current password"), {
      target: { value: "correct-pass" },
    });
    fireEvent.change(screen.getByPlaceholderText("New email"), {
      target: { value: "renamed@example.com" },
    });
    // If a form is open, two "Save" buttons exist (display-name's own,
    // always present, plus this form's) -- this form's is the last one.
    fireEvent.click(screen.getAllByRole("button", { name: "Save" }).at(-1)!);

    await waitFor(() => expect(logoutCommunityMock).toHaveBeenCalled());
    expect(logoutDevMock).not.toHaveBeenCalled();
    // logoutCommunity owns its own reload/SSO-redirect (see api.ts) -- a
    // caller-side reload here would race a pending SSO redirect and win,
    // silently cancelling it, so the caller must not also reload.
    expect(reloadMock).not.toHaveBeenCalled();
  });
});
