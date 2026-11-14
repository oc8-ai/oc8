// Wire types and hooks for HTTP API connections (transport "manual_http"):
// their logins, their declared tools, the test call and the tools editor.

import { api } from "@/lib/api";

/** The setup field and env name a login's one secret travels under. */
export const LOGIN_SECRET_FIELD = "auth_secret";
export const LOGIN_SECRET_ENV = "HTTP_AUTH_SECRET";

export type HttpLoginDraft =
  | { readonly kind: "none" }
  | {
      readonly kind: "api_key";
      readonly in: "header" | "query";
      readonly name: string;
      readonly secret: string;
    }
  | { readonly kind: "basic"; readonly username: string; readonly secret: string }
  | {
      readonly kind: "oauth2_client_credentials";
      readonly tokenUrl: string;
      readonly clientId: string;
      readonly scope: string;
      readonly secret: string;
    };

export const NO_LOGIN: HttpLoginDraft = { kind: "none" };

/** `config.auth` as stored on the connection: never the secret. */
export function loginToConfig(login: HttpLoginDraft): Record<string, unknown> | null {
  switch (login.kind) {
    case "none":
      return null;
    case "api_key":
      return { kind: "api_key", in: login.in, name: login.name };
    case "basic":
      return { kind: "basic", username: login.username };
    case "oauth2_client_credentials":
      return {
        kind: "oauth2_client_credentials",
        token_url: login.tokenUrl,
        client_id: login.clientId,
        ...(login.scope ? { scope: login.scope } : {}),
      };
  }
}

export function loginSecret(login: HttpLoginDraft): string {
  return login.kind === "none" ? "" : login.secret;
}

/** RFC 7617: a Basic username cannot contain a colon (the server would read
 * everything after the first one as the password). */
export function basicUsernameHasColon(login: HttpLoginDraft): boolean {
  return login.kind === "basic" && login.username.includes(":");
}

/** Whether the non-secret parts are complete enough to save. */
export function loginComplete(login: HttpLoginDraft): boolean {
  switch (login.kind) {
    case "none":
      return true;
    case "api_key":
      return login.name.trim() !== "" && login.secret !== "";
    case "basic":
      return login.username.trim() !== "" && !basicUsernameHasColon(login) && login.secret !== "";
    case "oauth2_client_credentials":
      return (
        /^https?:\/\//.test(login.tokenUrl.trim()) &&
        login.clientId.trim() !== "" &&
        login.secret !== ""
      );
  }
}

/** One test call of one declared tool; `excerpt` never carries a secret. */
export interface HttpTestCallResult {
  ok: boolean;
  status: number | null;
  durationMs: number;
  excerpt: string;
  truncated: boolean;
  errorClass: "auth" | "token" | "blocked" | "unreachable" | "http" | null;
  message: string;
}

export function postHttpTestCall(
  connectionId: string,
  tool: string,
  args: Record<string, unknown>,
): Promise<HttpTestCallResult> {
  return api.post<HttpTestCallResult>(`/mcp/connections/${connectionId}/test-call`, {
    tool,
    arguments: args,
  });
}
