// frontend/src/lib/passkey.ts
// Passkeys through the browser's own WebAuthn API: the server sends options
// as JSON with binary values base64url-encoded, `navigator.credentials` wants
// ArrayBuffers, and the answer goes back as JSON again (the shape of
// `PublicKeyCredential.toJSON()`, built by hand because not every browser
// has it yet).
import {
  passkeyAuthenticationOptions,
  passkeyRegistrationOptions,
  verifyPasskey,
  verifyPasskeyRegistration,
  type PasskeyRegistered,
  type TotpSessionResult,
} from "@/lib/totp";

type T = (en: string, de: string) => string;
type Json = Record<string, unknown>;

export function base64urlToBuffer(value: string): ArrayBuffer {
  const base64 = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

export function bufferToBase64url(value: ArrayBuffer | ArrayBufferView): string {
  const bytes =
    value instanceof ArrayBuffer
      ? new Uint8Array(value)
      : new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** Whether this browser can create and use passkeys at all. */
export function passkeySupported(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.PublicKeyCredential !== "undefined" &&
    typeof navigator !== "undefined" &&
    navigator.credentials !== undefined
  );
}

interface DescriptorJson {
  id: string;
  type: "public-key";
  transports?: AuthenticatorTransport[];
}

function descriptors(list: unknown): PublicKeyCredentialDescriptor[] {
  return ((list as DescriptorJson[] | undefined) ?? []).map((d) => ({
    ...d,
    id: base64urlToBuffer(d.id),
  }));
}

export function creationOptionsFromJson(options: Json): PublicKeyCredentialCreationOptions {
  const user = options.user as { id: string; name: string; displayName: string };
  return {
    ...(options as unknown as PublicKeyCredentialCreationOptions),
    challenge: base64urlToBuffer(options.challenge as string),
    user: { ...user, id: base64urlToBuffer(user.id) },
    excludeCredentials: descriptors(options.excludeCredentials),
  };
}

export function requestOptionsFromJson(options: Json): PublicKeyCredentialRequestOptions {
  return {
    ...(options as unknown as PublicKeyCredentialRequestOptions),
    challenge: base64urlToBuffer(options.challenge as string),
    allowCredentials: descriptors(options.allowCredentials),
  };
}

/** The parts of a `PublicKeyCredential` this page reads, so a test can pass
 *  a plain object for it. */
interface CredentialLike {
  id: string;
  rawId: ArrayBuffer;
  type: string;
  authenticatorAttachment?: string | null;
  response: {
    clientDataJSON: ArrayBuffer;
    attestationObject?: ArrayBuffer;
    getTransports?: () => string[];
    authenticatorData?: ArrayBuffer;
    signature?: ArrayBuffer;
    userHandle?: ArrayBuffer | null;
  };
  getClientExtensionResults: () => AuthenticationExtensionsClientOutputs;
}

export function credentialToJson(credential: CredentialLike): Json {
  const r = credential.response;
  const response: Json = { clientDataJSON: bufferToBase64url(r.clientDataJSON) };
  if (r.attestationObject) {
    response.attestationObject = bufferToBase64url(r.attestationObject);
    response.transports = r.getTransports?.() ?? [];
  } else {
    response.authenticatorData = bufferToBase64url(r.authenticatorData ?? new ArrayBuffer(0));
    response.signature = bufferToBase64url(r.signature ?? new ArrayBuffer(0));
    response.userHandle = r.userHandle ? bufferToBase64url(r.userHandle) : null;
  }
  return {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment ?? null,
    response,
    clientExtensionResults: credential.getClientExtensionResults(),
  };
}

class PasskeyCancelled extends Error {}

function answered(credential: Credential | null): CredentialLike {
  if (credential === null) throw new PasskeyCancelled();
  return credential as unknown as CredentialLike;
}

/** Create a passkey in the browser and register it under `label`. */
export async function registerPasskey(token: string, label: string): Promise<PasskeyRegistered> {
  const { options } = await passkeyRegistrationOptions(token);
  const credential = answered(
    await navigator.credentials.create({ publicKey: creationOptionsFromJson(options) }),
  );
  return verifyPasskeyRegistration(token, credentialToJson(credential), label);
}

/** Answer the login challenge with one of the member's passkeys. */
export async function signInWithPasskey(token: string): Promise<TotpSessionResult> {
  const { options } = await passkeyAuthenticationOptions(token);
  const credential = answered(
    await navigator.credentials.get({ publicKey: requestOptionsFromJson(options) }),
  );
  return verifyPasskey(token, credentialToJson(credential));
}

/** What to tell the member when a passkey step fails. The browser's own
 *  messages are technical; the server's are written for people. */
export function passkeyErrorMessage(error: unknown, t: T): string {
  // A DOMException is not an Error in every engine: read its name directly.
  const name =
    typeof error === "object" && error !== null && "name" in error
      ? String((error as { name: unknown }).name)
      : "";
  if (error instanceof PasskeyCancelled || name === "NotAllowedError" || name === "AbortError") {
    return t(
      "The passkey prompt was cancelled or timed out.",
      "Die Passkey-Abfrage wurde abgebrochen oder ist abgelaufen.",
    );
  }
  if (name === "InvalidStateError") {
    return t("This passkey is already registered.", "Dieser Passkey ist bereits registriert.");
  }
  if (name === "SecurityError" || name === "NotSupportedError") {
    return t(
      "This browser cannot use a passkey for this site.",
      "Dieser Browser kann für diese Seite keinen Passkey verwenden.",
    );
  }
  return error instanceof Error && error.message
    ? error.message
    : t("The passkey could not be used.", "Der Passkey konnte nicht verwendet werden.");
}
