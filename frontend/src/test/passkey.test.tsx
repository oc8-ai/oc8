// frontend/src/test/passkey.test.tsx
// Passkeys at login: the challenge answered with a passkey, enrolling one
// when the organisation asks for a second factor, and the base64url
// conversion between the server's JSON and the browser's WebAuthn API.
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { toastErrorMock } = vi.hoisted(() => ({ toastErrorMock: vi.fn() }));

vi.mock("@/lib/totp", () => ({
  verifyTotp: vi.fn(async () => ({ token: "totp-session-token", principal: {}, memberId: "m1" })),
  sendEmailCode: vi.fn(),
  verifyEmailCode: vi.fn(),
  enrollTotp: vi.fn(async () => ({ secret: "ABCDEFGH", provisioningUri: "otpauth://totp/x" })),
  confirmTotp: vi.fn(),
  renderTotpQrDataUrl: vi.fn(async () => "data:image/png;base64,fake"),
  enrollEmailCode: vi.fn(),
  confirmEmailCode: vi.fn(),
  passkeyRegistrationOptions: vi.fn(async () => ({
    options: {
      rp: { id: "localhost", name: "oc8" },
      user: { id: "AQID", name: "ada@example.com", displayName: "Ada" },
      challenge: "AAEC",
      pubKeyCredParams: [{ type: "public-key", alg: -7 }],
      excludeCredentials: [{ id: "BAUG", type: "public-key", transports: ["usb"] }],
      timeout: 300000,
    },
  })),
  verifyPasskeyRegistration: vi.fn(async () => ({ id: "pk-1", backupCodes: ["CODE-1", "CODE-2"] })),
  passkeyAuthenticationOptions: vi.fn(async () => ({
    options: {
      challenge: "AAEC",
      rpId: "localhost",
      allowCredentials: [{ id: "AQID", type: "public-key" }],
      timeout: 300000,
    },
  })),
  verifyPasskey: vi.fn(async () => ({
    token: "passkey-session-token",
    principal: {},
    memberId: "m1",
  })),
}));
vi.mock("sonner", async (importOriginal) => {
  const actual = await importOriginal<typeof import("sonner")>();
  return { ...actual, toast: { ...actual.toast, error: toastErrorMock } };
});

import { TotpChallenge } from "@/components/totp-challenge";
import { SecondFactorEnroll } from "@/components/second-factor";
import { base64urlToBuffer, bufferToBase64url } from "@/lib/passkey";
import {
  passkeyAuthenticationOptions,
  verifyPasskey,
  verifyPasskeyRegistration,
  verifyTotp,
} from "@/lib/totp";

function buffer(bytes: number[]): ArrayBuffer {
  return new Uint8Array(bytes).buffer;
}

function fakeAssertion() {
  return {
    id: "AQID",
    rawId: buffer([1, 2, 3]),
    type: "public-key",
    authenticatorAttachment: "cross-platform",
    response: {
      clientDataJSON: buffer([4, 5]),
      authenticatorData: buffer([6, 7]),
      signature: buffer([8, 9]),
      userHandle: null,
    },
    getClientExtensionResults: () => ({}),
  };
}

function fakeAttestation() {
  return {
    id: "AQID",
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

const credentials = { create: vi.fn(), get: vi.fn() };

beforeEach(() => {
  credentials.create.mockReset();
  credentials.get.mockReset();
  toastErrorMock.mockReset();
  vi.mocked(verifyPasskey).mockClear();
  vi.mocked(verifyTotp).mockClear();
  vi.mocked(passkeyAuthenticationOptions).mockClear();
  vi.mocked(verifyPasskeyRegistration).mockClear();
  Object.defineProperty(navigator, "credentials", { value: credentials, configurable: true });
  Object.defineProperty(window, "PublicKeyCredential", {
    value: function PublicKeyCredential() {},
    configurable: true,
  });
});

afterEach(() => {
  Reflect.deleteProperty(window, "PublicKeyCredential");
});

describe("base64url", () => {
  it("round-trips bytes without padding", () => {
    const bytes = [0, 1, 2, 250, 251, 252, 253, 254, 255];
    const encoded = bufferToBase64url(buffer(bytes));
    expect(encoded).not.toMatch(/[=+/]/);
    expect(Array.from(new Uint8Array(base64urlToBuffer(encoded)))).toEqual(bytes);
  });
});

describe("TotpChallenge with a passkey", () => {
  it("signs in with the passkey and reports the session", async () => {
    credentials.get.mockResolvedValue(fakeAssertion());
    const onVerified = vi.fn();
    render(
      <TotpChallenge
        token="challenge-token"
        methods={["totp", "passkey"]}
        onVerified={onVerified}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /passkey or security key/i }));
    fireEvent.click(screen.getByRole("button", { name: /^use passkey$/i }));

    await waitFor(() => expect(onVerified).toHaveBeenCalledWith("passkey-session-token"));
    expect(passkeyAuthenticationOptions).toHaveBeenCalledWith("challenge-token");
    const request = credentials.get.mock.calls[0][0].publicKey;
    expect(Array.from(new Uint8Array(request.challenge))).toEqual([0, 1, 2]);
    expect(Array.from(new Uint8Array(request.allowCredentials[0].id))).toEqual([1, 2, 3]);
    expect(request.rpId).toBe("localhost");
    expect(verifyPasskey).toHaveBeenCalledWith("challenge-token", {
      id: "AQID",
      rawId: "AQID",
      type: "public-key",
      authenticatorAttachment: "cross-platform",
      response: {
        clientDataJSON: "BAU",
        authenticatorData: "Bgc",
        signature: "CAk",
        userHandle: null,
      },
      clientExtensionResults: {},
    });
    expect(verifyTotp).not.toHaveBeenCalled();
  });

  it("offers the passkey alone when it is the only method", () => {
    render(<TotpChallenge token="challenge-token" methods={["passkey"]} onVerified={vi.fn()} />);

    expect(screen.getByRole("button", { name: /^use passkey$/i })).toBeInTheDocument();
    expect(screen.queryByLabelText(/^code$/i)).toBeNull();
  });

  it("says so when the browser prompt is cancelled", async () => {
    credentials.get.mockRejectedValue(new DOMException("cancelled", "NotAllowedError"));
    const onVerified = vi.fn();
    render(<TotpChallenge token="challenge-token" methods={["passkey"]} onVerified={onVerified} />);

    fireEvent.click(screen.getByRole("button", { name: /^use passkey$/i }));

    await waitFor(() =>
      expect(toastErrorMock).toHaveBeenCalledWith(expect.stringMatching(/cancelled|timed out/i)),
    );
    expect(verifyPasskey).not.toHaveBeenCalled();
    expect(onVerified).not.toHaveBeenCalled();
  });

  it("keeps backup codes available when a passkey is the method", async () => {
    const onVerified = vi.fn();
    render(<TotpChallenge token="challenge-token" methods={["passkey"]} onVerified={onVerified} />);

    fireEvent.click(screen.getByRole("button", { name: /use a backup code/i }));
    fireEvent.change(screen.getByLabelText(/backup code/i), { target: { value: "ABCDE-12345" } });
    fireEvent.click(screen.getByRole("button", { name: /verify/i }));

    await waitFor(() => expect(onVerified).toHaveBeenCalledWith("totp-session-token"));
    expect(verifyTotp).toHaveBeenCalledWith("challenge-token", "ABCDE-12345");
  });
});

describe("Enrolling a passkey at login", () => {
  it("creates a named passkey and shows the backup codes", async () => {
    credentials.create.mockResolvedValue(fakeAttestation());
    const onDone = vi.fn();
    render(
      <SecondFactorEnroll token="enrol-token" methods={["totp", "passkey"]} onDone={onDone} />,
    );

    fireEvent.click(screen.getByRole("button", { name: /passkey or security key/i }));
    fireEvent.change(screen.getByLabelText(/name this passkey/i), {
      target: { value: "Laptop" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^create passkey$/i }));

    expect(await screen.findByText("CODE-1")).toBeInTheDocument();
    const options = credentials.create.mock.calls[0][0].publicKey;
    expect(Array.from(new Uint8Array(options.user.id))).toEqual([1, 2, 3]);
    expect(Array.from(new Uint8Array(options.excludeCredentials[0].id))).toEqual([4, 5, 6]);
    expect(verifyPasskeyRegistration).toHaveBeenCalledWith(
      "enrol-token",
      expect.objectContaining({
        id: "AQID",
        response: { clientDataJSON: "BAU", attestationObject: "Bgc", transports: ["internal"] },
      }),
      "Laptop",
    );
  });

  it("says so when the browser cannot use passkeys", () => {
    Reflect.deleteProperty(window, "PublicKeyCredential");
    render(<SecondFactorEnroll token="enrol-token" methods={["passkey"]} onDone={vi.fn()} />);

    expect(screen.getByText(/this browser cannot use passkeys/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^create passkey$/i })).toBeDisabled();
  });
});
