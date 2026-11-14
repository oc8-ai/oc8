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

/** One declared tool on the wire (`GET`/`PATCH .../http-tools`). */
export interface HttpToolWire {
  name: string;
  description: string;
  method: string;
  urlTemplate: string;
  paramSchema: Record<string, unknown>;
  paramLocations?: Record<string, "path" | "query" | "header" | "body">;
  bodyParam?: string | null;
  group?: string;
}

export interface HttpToolScopes {
  read: string[];
  modify: string[];
}

/** Where a connection's tools were imported from, for re-reading later. */
export interface ImportMetadata {
  sourceUrl: string | null;
  fingerprint: string;
  importedAt: string;
  title: string;
  version: string;
}

export interface UpdateHttpToolsBody {
  httpTools: HttpToolWire[];
  scopes: HttpToolScopes;
  /** Omitted keeps the stored import metadata; null removes it. */
  openapi?: ImportMetadata | null;
}

export interface HttpToolsSnapshot {
  connectionId: string;
  name: string;
  baseUrl: string;
  httpTools: HttpToolWire[];
  scopes: HttpToolScopes;
  auth: {
    kind: string;
    in: string | null;
    name: string | null;
    username: string | null;
    tokenUrl: string | null;
    clientId: string | null;
    scope: string | null;
  } | null;
  openapi: ImportMetadata | null;
}

/** Replace a connection's tools, their read/modify classification and its
 * import metadata, here and in the manifest it was installed from. */
export function patchHttpTools(
  connectionId: string,
  body: UpdateHttpToolsBody,
): Promise<HttpToolsSnapshot> {
  return api.patch<HttpToolsSnapshot>(`/mcp/connections/${connectionId}/http-tools`, body);
}

type Json = Record<string, unknown>;

function isObject(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function firstConnection(manifest: Json): Json | null {
  const pack = manifest.tool_pack;
  if (!isObject(pack) || !Array.isArray(pack.connections)) return null;
  const conn: unknown = pack.connections[0];
  return isObject(conn) ? conn : null;
}

/** The manifest without what `PATCH .../http-tools` can replace in place. */
function withoutHttpTools(manifest: Json): Json {
  const conn = firstConnection(manifest);
  if (!conn) return manifest;
  const { scopes: _scopes, config, ...rest } = conn;
  const {
    http_tools: _tools,
    openapi: _openapi,
    ...otherConfig
  } = isObject(config) ? config : ({} as Json);
  const pack = manifest.tool_pack as Json;
  return {
    ...manifest,
    tool_pack: {
      ...pack,
      connections: [{ ...rest, config: otherConfig }, ...(pack.connections as unknown[]).slice(1)],
    },
  };
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((x): x is string => typeof x === "string") : [];
}

/** When `next` differs from the installed `previous` manifest only in its HTTP
 * API connection's tools, read/modify classification or import metadata, the
 * body that replaces them in place; otherwise (a new name, base URL, login,
 * anything else) null, and the manifest has to be installed again. */
export function httpToolsUpdateBetween(previous: Json, next: Json): UpdateHttpToolsBody | null {
  const conn = firstConnection(next);
  if (!conn || conn.transport !== "manual_http") return null;
  if (JSON.stringify(withoutHttpTools(previous)) !== JSON.stringify(withoutHttpTools(next))) {
    return null;
  }
  const config = isObject(conn.config) ? conn.config : {};
  const tools = Array.isArray(config.http_tools) ? config.http_tools.filter(isObject) : [];
  const scopes = isObject(conn.scopes) ? conn.scopes : {};
  const meta = isObject(config.openapi) ? config.openapi : null;
  const body: UpdateHttpToolsBody = {
    httpTools: tools.map((tool) => ({
      name: String(tool.name ?? ""),
      description: String(tool.description ?? ""),
      method: String(tool.method ?? "GET"),
      urlTemplate: String(tool.url_template ?? ""),
      paramSchema: isObject(tool.param_schema) ? tool.param_schema : {},
      ...(isObject(tool.param_locations)
        ? {
            paramLocations: tool.param_locations as HttpToolWire["paramLocations"],
          }
        : {}),
      ...(typeof tool.body_param === "string" ? { bodyParam: tool.body_param } : {}),
      ...(typeof tool.group === "string" && tool.group ? { group: tool.group } : {}),
    })),
    scopes: { read: stringList(scopes.read), modify: stringList(scopes.modify) },
    openapi: meta
      ? {
          sourceUrl: typeof meta.source_url === "string" ? meta.source_url : null,
          fingerprint: String(meta.fingerprint ?? ""),
          importedAt: String(meta.imported_at ?? ""),
          title: String(meta.title ?? ""),
          version: String(meta.version ?? ""),
        }
      : null,
  };
  return body;
}

/** Why a tool cannot be saved, by the same rules the server applies when it
 * stores an HTTP API's tools (`HttpToolSpec`/`UpdateHttpToolsRequest`). */
export type HttpToolProblem =
  | "name_missing"
  | "name_invalid"
  | "name_duplicate"
  | "path"
  | "schema_json"
  | "schema_object"
  | "schema_ref"
  | "schema_size";

export const HTTP_TOOL_NAME = /^[A-Za-z0-9_-]{1,64}$/;
/** Measured as compact UTF-8 JSON bytes, as the server measures it
 * (`schemas/requests.py`, PARAM_SCHEMA_MAX_BYTES), so both refuse the same schema. */
export const PARAM_SCHEMA_MAX_BYTES = 64_000;
const PARAM_SCHEMA_MAX_DEPTH = 32;

/** The fields of a tool as an editor holds them: the schema still as text. */
export interface HttpToolFields {
  readonly name: string;
  readonly urlTemplate: string;
  /** JSON text; empty means "no parameters". */
  readonly paramSchema: string;
}

function schemaProblem(text: string): HttpToolProblem | null {
  if (!text.trim()) return null;
  let schema: unknown;
  try {
    schema = JSON.parse(text);
  } catch {
    return "schema_json";
  }
  if (!isObject(schema) || (schema.type !== undefined && schema.type !== "object")) {
    return "schema_object";
  }
  if (new TextEncoder().encode(JSON.stringify(schema)).length > PARAM_SCHEMA_MAX_BYTES) {
    return "schema_size";
  }
  const stack: Array<[unknown, number]> = [[schema, 1]];
  while (stack.length > 0) {
    const [node, depth] = stack.pop()!;
    if (depth > PARAM_SCHEMA_MAX_DEPTH) return "schema_size";
    if (Array.isArray(node)) {
      for (const child of node) stack.push([child, depth + 1]);
    } else if (isObject(node)) {
      const ref = node.$ref;
      if (ref !== undefined && !(typeof ref === "string" && ref.startsWith("#"))) {
        return "schema_ref";
      }
      for (const child of Object.values(node)) stack.push([child, depth + 1]);
    }
  }
  return null;
}

/** One problem (or null) per tool, in order: the first rule each one breaks. */
export function validateHttpTools(tools: readonly HttpToolFields[]): Array<HttpToolProblem | null> {
  const seen = new Map<string, number>();
  for (const tool of tools) seen.set(tool.name, (seen.get(tool.name) ?? 0) + 1);
  return tools.map((tool) => {
    if (tool.name === "") return "name_missing";
    if (!HTTP_TOOL_NAME.test(tool.name)) return "name_invalid";
    if ((seen.get(tool.name) ?? 0) > 1) return "name_duplicate";
    const path = tool.urlTemplate;
    if (!path.startsWith("/") || path.startsWith("//") || path.includes("://")) return "path";
    return schemaProblem(tool.paramSchema);
  });
}
