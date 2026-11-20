// frontend/src/lib/totp.ts
// Thin client for the second-factor endpoints: the three shared TOTP ones,
// the email code, passkeys, and removing one of your own methods. Deliberately raw fetch,
// not the authenticated `api.*` client (frontend/src/lib/api.ts) -- these
// calls happen with a narrow enrollment/challenge token or mid-login,
// before a real session exists, exactly like login.tsx's own
// handleLogin/handleSetup already do.

import QRCode from "qrcode";

const API_URL =
  (import.meta.env.VITE_API_URL as string | undefined) ?? "http://localhost:8099/api/v1";

export interface TotpEnrollResult {
  secret: string;
  provisioningUri: string;
}

export interface TotpConfirmResult {
  backupCodes: string[];
}

export interface TotpSessionResult {
  token: string;
  principal: unknown;
  memberId: string;
  totpGraceExpiresAt?: string | null;
  requiresTotpCode?: boolean;
  requiresTotpEnrollment?: boolean;
  /** Beside `requiresTotpCode`: the methods the member may answer with.
   *  Beside `requiresTotpEnrollment`: the methods they may enrol in. */
  twoFactorMethods?: string[];
}

/** A second-factor method. */
export type TwoFactorMethod = "totp" | "email" | "passkey";

/** WebAuthn options as JSON, binary values base64url-encoded; `lib/passkey`
 *  turns them into what `navigator.credentials` takes. */
export interface PasskeyOptionsResult {
  options: Record<string, unknown>;
}

export interface PasskeyRegistered {
  id: string;
  /** Shown once. Empty when the member already had backup codes. */
  backupCodes: string[];
}

export interface EmailCodeSent {
  /** The address the code went to, so the screen can say where to look. */
  sentTo: string;
}

async function send(method: string, path: string, token: string, body?: unknown) {
  const res = await fetch(`${API_URL}${path}`, {
    method,
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${token}`,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    const error = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(typeof error.detail === "string" ? error.detail : "TOTP request failed");
  }
  return res;
}

async function post<T>(path: string, token: string, body?: unknown): Promise<T> {
  const res = await send("POST", path, token, body);
  return (await res.json()) as T;
}

export function enrollTotp(token: string): Promise<TotpEnrollResult> {
  return post<TotpEnrollResult>("/auth/totp/enroll", token);
}

export function confirmTotp(
  token: string,
  secret: string,
  code: string,
): Promise<TotpConfirmResult> {
  return post<TotpConfirmResult>("/auth/totp/confirm", token, { secret, code });
}

export function verifyTotp(token: string, code: string): Promise<TotpSessionResult> {
  return post<TotpSessionResult>("/auth/totp/verify", token, { code });
}

/** Mail an enrolment code to the caller's sign-in address. */
export function enrollEmailCode(token: string): Promise<EmailCodeSent> {
  return post<EmailCodeSent>("/auth/two-factor/email/enroll", token);
}

/** Enrol the email method; backup codes come back only if the member had none. */
export function confirmEmailCode(token: string, code: string): Promise<TotpConfirmResult> {
  return post<TotpConfirmResult>("/auth/two-factor/email/confirm", token, { code });
}

/** Mail a login code (challenge token). */
export function sendEmailCode(token: string): Promise<EmailCodeSent> {
  return post<EmailCodeSent>("/auth/two-factor/email/send", token);
}

/** Answer the login challenge with the mailed code; mints the session. */
export function verifyEmailCode(token: string, code: string): Promise<TotpSessionResult> {
  return post<TotpSessionResult>("/auth/two-factor/email/verify", token, { code });
}

/** Start registering a passkey (full session or enrolment token). */
export function passkeyRegistrationOptions(token: string): Promise<PasskeyOptionsResult> {
  return post<PasskeyOptionsResult>("/auth/two-factor/passkey/register/options", token);
}

/** Register the passkey the browser created, under the member's name for it. */
export function verifyPasskeyRegistration(
  token: string,
  credential: Record<string, unknown>,
  label: string,
): Promise<PasskeyRegistered> {
  return post<PasskeyRegistered>("/auth/two-factor/passkey/register/verify", token, {
    credential,
    label,
  });
}

/** A sign-in challenge for the member's passkeys (challenge token). */
export function passkeyAuthenticationOptions(token: string): Promise<PasskeyOptionsResult> {
  return post<PasskeyOptionsResult>("/auth/two-factor/passkey/authenticate/options", token);
}

/** Answer the login challenge with a passkey; mints the session. */
export function verifyPasskey(
  token: string,
  credential: Record<string, unknown>,
): Promise<TotpSessionResult> {
  return post<TotpSessionResult>("/auth/two-factor/passkey/authenticate/verify", token, {
    credential,
  });
}

export async function renamePasskey(token: string, id: string, label: string): Promise<void> {
  await send("PATCH", `/auth/two-factor/passkey/${encodeURIComponent(id)}`, token, { label });
}

/** Remove one passkey; raw fetch for the same reason as `removeSecondFactor`. */
export async function removePasskey(
  token: string,
  id: string,
  currentPassword: string,
): Promise<void> {
  await send("DELETE", `/auth/two-factor/passkey/${encodeURIComponent(id)}`, token, {
    currentPassword,
  });
}

/** Remove one of the caller's own methods. Raw fetch for the same reason as
 *  the rest of this file, and because a wrong password answers 401, which the
 *  shared client would take for an expired session and sign out. */
export async function removeSecondFactor(
  token: string,
  method: string,
  currentPassword: string,
): Promise<void> {
  await send("DELETE", `/auth/two-factor/methods/${method}`, token, { currentPassword });
}

export function renderTotpQrDataUrl(provisioningUri: string): Promise<string> {
  return QRCode.toDataURL(provisioningUri);
}
