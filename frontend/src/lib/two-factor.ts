// frontend/src/lib/two-factor.ts
// Plain helpers about second-factor methods, shared by the login screens,
// the profile page and the settings page.
import type { TwoFactorMethod } from "@/lib/totp";

type T = (en: string, de: string) => string;

/** The methods a sign-in may use, from the login response. An older or
 *  TOTP-only response carries none: the authenticator app it is. */
export function loginMethods(methods: string[] | undefined): TwoFactorMethod[] {
  const known = (methods ?? []).filter((m): m is TwoFactorMethod => m === "totp" || m === "email");
  return known.length > 0 ? known : ["totp"];
}

export function methodLabel(method: string, t: T): string {
  switch (method) {
    case "totp":
      return t("Authenticator app", "Authenticator-App");
    case "email":
      return t("Code by email", "Code per E-Mail");
    case "passkey":
      return t("Passkey or security key", "Passkey oder Sicherheitsschlüssel");
    default:
      return method;
  }
}

/** One code a minute: the server refuses a resend sooner. */
export const RESEND_COOLDOWN_SECONDS = 60;
