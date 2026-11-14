// Wire types and hooks for HTTP API connections (transport "manual_http"):
// their logins, their declared tools, the test call and the tools editor.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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

export type HttpMethod = "GET" | "HEAD" | "POST" | "PUT" | "PATCH" | "DELETE";
export type ParamLocation = "path" | "query" | "header" | "body";

/** One endpoint as an agent tool (a connection's `http_tools` entry), as
 * `GET`/`PATCH .../http-tools` carry it. */
export interface HttpToolDef {
  readonly name: string;
  readonly description: string;
  readonly method: HttpMethod;
  readonly urlTemplate: string;
  readonly paramSchema: Record<string, unknown>;
  readonly paramLocations?: Readonly<Record<string, ParamLocation>>;
  readonly bodyParam?: string | null;
  readonly group?: string;
}

/** Which tools only read and which change something (the department's tool
 * policy decides what each may do). */
export interface ToolScopes {
  readonly read: readonly string[];
  readonly modify: readonly string[];
}

/** Where a connection's tools were imported from, for re-reading later. */
export interface ImportMetadata {
  readonly sourceUrl: string | null;
  readonly fingerprint: string;
  readonly importedAt: string;
  readonly title: string;
  readonly version: string;
}

/** A stored login without its secret. */
export interface HttpLoginShape {
  readonly kind: string;
  readonly in?: "header" | "query" | null;
  readonly name?: string | null;
  readonly username?: string | null;
  readonly tokenUrl?: string | null;
  readonly clientId?: string | null;
  readonly scope?: string | null;
}

export interface HttpToolsSnapshot {
  readonly connectionId: string;
  readonly name: string;
  readonly baseUrl: string;
  readonly httpTools: readonly HttpToolDef[];
  readonly scopes: ToolScopes;
  readonly auth: HttpLoginShape | null;
  readonly openapi: ImportMetadata | null;
}

/** What a custom server source hands the wizard. */
export interface CustomServerDraft {
  readonly baseUrl: string;
  readonly login: HttpLoginDraft;
  readonly httpTools: readonly HttpToolDef[];
  readonly scopes: ToolScopes;
  readonly openapi: ImportMetadata | null;
}

export interface UpdateHttpToolsBody {
  httpTools: readonly HttpToolDef[];
  scopes: ToolScopes;
  /** Omitted keeps the stored import metadata; null removes it. */
  openapi?: ImportMetadata | null;
}

/** The stored (snake_case) shape of one tool. */
export function toolToConfig(tool: HttpToolDef): Record<string, unknown> {
  return {
    name: tool.name,
    description: tool.description,
    method: tool.method,
    url_template: tool.urlTemplate,
    param_schema: tool.paramSchema,
    param_locations: { ...(tool.paramLocations ?? {}) },
    body_param: tool.bodyParam ?? null,
    ...(tool.group ? { group: tool.group } : {}),
  };
}

export function metadataToConfig(meta: ImportMetadata): Record<string, unknown> {
  return {
    source_url: meta.sourceUrl,
    fingerprint: meta.fingerprint,
    imported_at: meta.importedAt,
    title: meta.title,
    version: meta.version,
  };
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
      method: String(tool.method ?? "GET") as HttpMethod,
      urlTemplate: String(tool.url_template ?? ""),
      paramSchema: isObject(tool.param_schema) ? tool.param_schema : {},
      ...(isObject(tool.param_locations)
        ? {
            paramLocations: tool.param_locations as HttpToolDef["paramLocations"],
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

/** What to tell the person about a tool's problem, in their language. */
export function httpToolProblemText(
  problem: HttpToolProblem,
  t: (en: string, de?: string) => string,
): string {
  switch (problem) {
    case "name_missing":
      return t("Give this tool a name.", "Gib diesem Tool einen Namen.");
    case "name_invalid":
      return t(
        "Use only letters, digits, _ and - in a tool name (at most 64).",
        "Verwende im Tool-Namen nur Buchstaben, Ziffern, _ und - (höchstens 64).",
      );
    case "name_duplicate":
      return t("Another tool already has this name.", "Ein anderes Tool hat schon diesen Namen.");
    case "path":
      return t(
        "Enter the path below the base URL, starting with /.",
        "Gib den Pfad unterhalb der Basis-URL an, beginnend mit /.",
      );
    case "schema_json":
      return t(
        "The parameter schema is not valid JSON.",
        "Das Parameter-Schema ist kein gültiges JSON.",
      );
    case "schema_object":
      return t(
        'The parameter schema must be a JSON Schema object with "type": "object".',
        'Das Parameter-Schema muss ein JSON-Schema-Objekt mit "type": "object" sein.',
      );
    case "schema_ref":
      return t(
        "The parameter schema may only refer to itself ($ref starting with #).",
        "Das Parameter-Schema darf nur auf sich selbst verweisen ($ref beginnend mit #).",
      );
    case "schema_size":
      return t(
        "The parameter schema is too large or nested too deeply.",
        "Das Parameter-Schema ist zu groß oder zu tief verschachtelt.",
      );
  }
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

/** The edit form of a tool for `validateHttpTools`: its schema as JSON text. */
export function toolFields(tool: HttpToolDef): HttpToolFields {
  return {
    name: tool.name,
    urlTemplate: tool.urlTemplate,
    paramSchema: JSON.stringify(tool.paramSchema ?? {}),
  };
}

const httpToolsKey = (connectionId: string) => ["mcp", "http-tools", connectionId] as const;

export function useHttpTools(connectionId: string | null) {
  return useQuery<HttpToolsSnapshot>({
    queryKey: httpToolsKey(connectionId ?? ""),
    queryFn: () => api.get<HttpToolsSnapshot>(`/mcp/connections/${connectionId}/http-tools`),
    enabled: connectionId !== null,
  });
}

export function useUpdateHttpTools() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      connectionId: string;
      httpTools: readonly HttpToolDef[];
      scopes: ToolScopes;
      openapi: ImportMetadata | null;
    }) =>
      patchHttpTools(body.connectionId, {
        httpTools: body.httpTools,
        scopes: body.scopes,
        // null would remove the stored import metadata; a source that has
        // none to give leaves it as it is.
        ...(body.openapi ? { openapi: body.openapi } : {}),
      }),
    onSuccess: (snapshot) => {
      qc.setQueryData(httpToolsKey(snapshot.connectionId), snapshot);
      void qc.invalidateQueries({ queryKey: ["mcp", "connections"] });
    },
  });
}

/** The read/modify classification as the server checks it on
 * `PATCH .../http-tools`: every name a tool, and no tool in both lists
 * (`UpdateHttpToolsRequest`). Null when it holds. */
export function scopesProblem(
  tools: readonly { readonly name: string }[],
  scopes: ToolScopes,
): { readonly kind: "unknown" | "both"; readonly names: readonly string[] } | null {
  const names = new Set(tools.map((tool) => tool.name));
  const unknown = [...new Set([...scopes.read, ...scopes.modify])].filter((n) => !names.has(n));
  if (unknown.length > 0) return { kind: "unknown", names: unknown.sort() };
  const modify = new Set(scopes.modify);
  const both = [...new Set(scopes.read)].filter((n) => modify.has(n));
  if (both.length > 0) return { kind: "both", names: both.sort() };
  return null;
}

/** Why what a custom server source handed back cannot be used, in the
 * person's language, or null. `withConnection`: the draft also brings the base
 * URL and login (the wizard); editing an existing connection uses only its
 * tools, classification and import metadata. */
export function customServerDraftProblem(
  draft: CustomServerDraft,
  t: (en: string, de?: string) => string,
  { withConnection }: { withConnection: boolean },
): string | null {
  if (withConnection) {
    if (!/^https?:\/\/\S+$/.test(draft.baseUrl.trim())) {
      return t(
        "The API needs a base URL starting with http:// or https://.",
        "Die API braucht eine Basis-URL, die mit http:// oder https:// beginnt.",
      );
    }
    if (basicUsernameHasColon(draft.login)) {
      return t(
        "A Basic username cannot contain a colon.",
        "Ein Basic-Benutzername darf keinen Doppelpunkt enthalten.",
      );
    }
    if (!loginComplete(draft.login)) {
      return t(
        "The login is incomplete: fill in every field and the secret.",
        "Die Anmeldung ist unvollständig: Fülle alle Felder und das Geheimnis aus.",
      );
    }
  }
  if (draft.httpTools.length === 0) {
    return t("Choose at least one endpoint.", "Wähle mindestens einen Endpunkt.");
  }
  const problems = validateHttpTools(draft.httpTools.map(toolFields));
  const first = problems.findIndex((problem) => problem !== null);
  if (first >= 0) {
    return `${draft.httpTools[first].name || "?"}: ${httpToolProblemText(problems[first]!, t)}`;
  }
  const scopes = scopesProblem(draft.httpTools, draft.scopes);
  if (scopes?.kind === "unknown") {
    return t(
      `The read/modify choice names endpoints that are not there: ${scopes.names.join(", ")}`,
      `Die Auswahl Lesen/Ändern nennt Endpunkte, die es nicht gibt: ${scopes.names.join(", ")}`,
    );
  }
  if (scopes?.kind === "both") {
    return t(
      `An endpoint either reads or modifies, not both: ${scopes.names.join(", ")}`,
      `Ein Endpunkt liest oder ändert, nicht beides: ${scopes.names.join(", ")}`,
    );
  }
  return null;
}
