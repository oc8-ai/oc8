// Thin client for the oc8 control-plane API.
//
// Dev auth: there is no silent auto-login. `getToken()` returns whatever is
// stored and nothing else -- if nothing is stored, the caller finds out by
// the throw, not by quietly becoming org_admin. `<DevSignIn>` is the only
// thing that ever calls `/auth/dev-login` and stores what it gets back.

const API_URL =
  (import.meta.env.VITE_API_URL as string | undefined) ?? "http://localhost:8099/api/v1";
const TOKEN_KEY = "oc8-dev-token";
export const COMMUNITY_TOKEN_KEY = "oc8-community-token";

async function getToken(): Promise<string> {
  // Check for community token first (single-instance auth)
  const communityToken =
    typeof window !== "undefined" ? window.localStorage.getItem(COMMUNITY_TOKEN_KEY) : null;
  if (communityToken) return communityToken;
  // Fall back to dev token
  const stored = typeof window !== "undefined" ? window.localStorage.getItem(TOKEN_KEY) : null;
  if (stored) return stored;
  throw new Error("not signed in");
}

export function hasCommunitySession(): boolean {
  return typeof window !== "undefined" && window.localStorage.getItem(COMMUNITY_TOKEN_KEY) !== null;
}

/** Clears the local session and, if the identity provider has an external SSO
 *  session (e.g. Keycloak in oc8-saas), redirects to its logout endpoint --
 *  otherwise reloads in place, same as before this existed. Owns its own
 *  end-of-flow navigation: a caller-side reload right after would race the
 *  SSO redirect and win, silently cancelling it. */
export async function logoutCommunity(): Promise<void> {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(COMMUNITY_TOKEN_KEY);
  // /auth/config is unguarded, so this works with the token already gone.
  try {
    const res = await fetch(`${API_URL}/auth/config`);
    if (res.ok) {
      const config = (await res.json()) as { ssoLogoutUrl?: string | null };
      if (config.ssoLogoutUrl) {
        // Without post_logout_redirect_uri, Keycloak shows its own logout
        // confirmation page instead of returning here automatically.
        const separator = config.ssoLogoutUrl.includes("?") ? "&" : "?";
        window.location.href = `${config.ssoLogoutUrl}${separator}post_logout_redirect_uri=${encodeURIComponent(window.location.origin)}`;
        return;
      }
    }
  } catch {
    // Network hiccup on the way out -- the local session is already cleared.
  }
  window.location.reload();
}

export interface DevTenant {
  id: string;
  name: string;
  slug: string;
  region: string;
}

export interface DevMemberSeat {
  departmentId: string;
  departmentName: string;
  seatRole: string;
}

export interface DevMember {
  id: string;
  subject: string;
  displayName: string;
  allDepartments: boolean;
  seats: DevMemberSeat[];
  roleId: string | null;
  roleName: string;
}

/** The sole "am I signed in" signal in dev mode. No separate signed-out flag:
 * signing out removes the token, and its absence is what shows `<DevSignIn>`
 * (see routes/__root.tsx). Two flags that could disagree with each other was
 * a state this code no longer has to reason about. */
export function hasDevSession(): boolean {
  return typeof window !== "undefined" && window.localStorage.getItem(TOKEN_KEY) !== null;
}

export function logoutDev(): void {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(TOKEN_KEY);
}

export async function loginDev(opts: {
  tenantId?: string;
  subject?: string;
  role?: string;
}): Promise<void> {
  const body: Record<string, string> = {};
  if (opts.tenantId) body.tenantId = opts.tenantId;
  if (opts.subject) body.subject = opts.subject;
  if (opts.role) body.role = opts.role;
  const res = await fetch(`${API_URL}/auth/dev-login`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`dev-login failed: ${res.status}`);
  const data = (await res.json()) as { token: string };
  window.localStorage.setItem(TOKEN_KEY, data.token);
}

export async function listDevTenants(): Promise<DevTenant[]> {
  const res = await fetch(`${API_URL}/auth/dev-tenants`);
  if (!res.ok) throw new Error(`tenant list failed: ${res.status}`);
  return (await res.json()) as DevTenant[];
}

export async function listDevMembers(tenantId: string): Promise<DevMember[]> {
  // A plain query param, not a JSON body -- FastAPI does not camelCase these,
  // so it is `tenant_id` on the wire. See auth.py's `dev_members` docstring:
  // sending `tenantId` here silently returns ACME's list instead of a 422.
  const res = await fetch(`${API_URL}/auth/dev-members?tenant_id=${encodeURIComponent(tenantId)}`);
  if (!res.ok) throw new Error(`member list failed: ${res.status}`);
  return (await res.json()) as DevMember[];
}

export async function createDevTenant(
  body: Pick<DevTenant, "name" | "slug" | "region">,
): Promise<DevTenant> {
  const res = await fetch(`${API_URL}/auth/dev-tenants`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await toError(res);
  return (await res.json()) as DevTenant;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const token = await getToken();
  const res = await fetch(`${API_URL}${path}`, {
    method,
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${token}`,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 401) {
    // The token died (its TTL, or a role that stopped resolving) -- there is
    // nothing to silently retry with, since minting no longer happens
    // implicitly. Drop it and reload: <DevSignIn>/<CommunitySignIn> reappears
    // because `hasDevSession()`/`hasCommunitySession()` is now false, which is
    // a real screen a person can act on instead of every open query failing
    // red one at a time. Community mode is checked first, mirroring
    // `getToken()`'s own precedence above -- clearing the wrong key here left
    // the expired community token in place, so the reload just re-sent it and
    // got another 401, forever.
    if (hasCommunitySession()) {
      // Owns its own reload/SSO-redirect -- see its doc comment.
      await logoutCommunity();
    } else {
      logoutDev();
      if (typeof window !== "undefined") window.location.reload();
    }
    throw new Error("session expired — signing in again");
  }
  if (!res.ok) throw await toError(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// Carries the HTTP status code alongside the (already user-facing) detail
// message, so a caller that needs to distinguish e.g. a 403 (permission
// denial -- an honest "you can't see this" message) from a 404 or a network
// failure (a generic "couldn't load" message) can do so without parsing
// `.message` text. Every existing call site keeps working unchanged: they
// only ever read `.message`, and this is still a plain `Error` to them.
export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function toError(res: Response): Promise<Error> {
  let detail: unknown;
  try {
    detail = (await res.json()).detail;
  } catch {
    detail = res.statusText;
  }
  return new ApiError(typeof detail === "string" ? detail : JSON.stringify(detail), res.status);
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  delete: <T>(path: string) => request<T>("DELETE", path),
};

/** For the handful of routes with no session at all: `/auth/password/forgot`,
 *  `/auth/password/reset`, `/auth/email/confirm`. `request()` (and so every
 *  `api.*` method) opens with `getToken()`, which THROWS "not signed in" when
 *  nothing is stored -- exactly the state of somebody who followed a
 *  "forgot password" link because they cannot log in. Going through `api.post`
 *  here would mean the request never leaves the browser for that caller. These
 *  three are `unguarded` on the backend for the same reason (see `auth.py`):
 *  the mailed token (or nothing, for the forgot-password ask) IS the
 *  credential, so no bearer header belongs on the request in the first place. */
export async function publicPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await toError(res);
  if (res.status === 204 || res.status === 202) return undefined as T;
  return (await res.json()) as T;
}

/** `/auth/sso/exchange` is a service_routers() route with NO `/api/v1`
 *  prefix (see oc8-enterprise's SAML SSO design spec §1) -- the one-time
 *  code IS the credential, same category as `publicPost`'s routes, but the
 *  URL needs `/api/v1` stripped back off `API_URL` first. */
const ROOT_API_URL = API_URL.replace(/\/api\/v1\/?$/, "");

export interface SsoExchangeResponse {
  token: string;
  /** True when `token` is a NARROW totp:challenge-scoped token rather than a
   *  session: the SSO callback must render the TOTP challenge instead of
   *  storing it. Same field the password login's own response carries for
   *  the same decision -- both screens branch on it identically. */
  requiresTotpCode?: boolean;
  /** True when `token` is a NARROW totp:enroll-scoped token: the member owes
   *  us an enrollment before any session exists. */
  requiresTotpEnrollment?: boolean;
}

export async function exchangeSsoCode(code: string): Promise<SsoExchangeResponse> {
  const res = await fetch(`${ROOT_API_URL}/auth/sso/exchange`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ code }),
  });
  if (!res.ok) throw await toError(res);
  return (await res.json()) as SsoExchangeResponse;
}

export interface SsoSamlConfig {
  enabled: boolean;
  idpEntityId: string;
  idpSsoUrl: string;
  defaultRoleId: string;
  emailAttribute: string;
  displayNameAttribute: string;
  spEntityId: string;
  acsUrl: string;
}

export interface SsoSamlConfigInput {
  metadataXml: string;
  defaultRoleId: string;
  emailAttribute?: string;
  displayNameAttribute?: string;
  enabled?: boolean;
}

export async function getSsoSamlConfig(): Promise<SsoSamlConfig | null> {
  const res = await fetch(`${API_URL}/sso/saml`, {
    headers: { authorization: `Bearer ${await getToken()}` },
  });
  if (!res.ok) throw await toError(res);
  return (await res.json()) as SsoSamlConfig | null;
}

export async function putSsoSamlConfig(input: SsoSamlConfigInput): Promise<SsoSamlConfig> {
  const res = await fetch(`${API_URL}/sso/saml`, {
    method: "PUT",
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${await getToken()}`,
    },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw await toError(res);
  return (await res.json()) as SsoSamlConfig;
}

// ---- Company backup / restore -----------------------------------------------
// These three don't fit `api.*`: export returns a binary blob, and preview /
// restore send multipart/form-data (a file), not JSON. `BackupPreviewDTO` and
// `BackupRestoreResultDTO` mirror `oc8.api.v1.backup`'s Pydantic models
// field-for-field -- those two DTOs are plain `BaseModel`, not `CamelModel`,
// so the wire (and these types) stay snake_case on purpose.

export interface BackupTableCount {
  archive: number;
  current: number;
}

export interface BackupPreviewDTO {
  manifest: Record<string, unknown>;
  table_counts: Record<string, BackupTableCount>;
  has_secrets: boolean;
  problems: string[];
}

export interface BackupRestoreResultDTO {
  tables: Record<string, number>;
  secrets_restored: number;
  excluded: string[];
}

export interface BackupExportResult {
  blob: Blob;
  filename: string;
}

export async function exportBackup(passphrase?: string): Promise<BackupExportResult> {
  const token = await getToken();
  const res = await fetch(`${API_URL}/backup/export`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ passphrase: passphrase ?? null }),
  });
  if (!res.ok) throw await toError(res);
  const disposition = res.headers.get("content-disposition") ?? "";
  const match = /filename="?([^";]+)"?/i.exec(disposition);
  return { blob: await res.blob(), filename: match?.[1] ?? "backup.tar.gz" };
}

// A plain <img src="/api/v1/capas/x/icon"> can't carry the bearer token,
// so the Capas page fetches the image itself and turns it into an object
// URL -- same reason exportBackup() above fetches instead of linking.
// `null` means "this Capa declares no icon" (a 404), not an error.
export async function fetchCapaIcon(pluginId: string): Promise<Blob | null> {
  const token = await getToken();
  const res = await fetch(`${API_URL}/capas/${pluginId}/icon`, {
    headers: { authorization: `Bearer ${token}` },
  });
  if (res.status === 404) return null;
  if (!res.ok) throw await toError(res);
  return res.blob();
}

// GET /files/{id} answers `Content-Disposition: attachment` with no filename
// (see files.py's download_file -- it never trusted the client-declared name
// for the header), so the caller supplies the filename it already knows from
// the owning FileAttachmentDTO/WorkspaceFileDTO rather than parsing one back
// out of the response.
export async function downloadFileAttachment(attachmentId: string): Promise<Blob> {
  const token = await getToken();
  const res = await fetch(`${API_URL}/files/${attachmentId}`, {
    headers: { authorization: `Bearer ${token}` },
  });
  if (!res.ok) throw await toError(res);
  return res.blob();
}

export async function previewBackup(file: File): Promise<BackupPreviewDTO> {
  const token = await getToken();
  const body = new FormData();
  body.append("file", file);
  const res = await fetch(`${API_URL}/backup/preview`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}` },
    body,
  });
  if (!res.ok) throw await toError(res);
  return (await res.json()) as BackupPreviewDTO;
}

export async function restoreBackup(
  file: File,
  confirmName: string,
  passphrase?: string,
): Promise<BackupRestoreResultDTO> {
  const token = await getToken();
  const body = new FormData();
  body.append("file", file);
  body.append("confirm_name", confirmName);
  if (passphrase) body.append("passphrase", passphrase);
  const res = await fetch(`${API_URL}/backup/restore`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}` },
    body,
  });
  if (!res.ok) throw await toError(res);
  return (await res.json()) as BackupRestoreResultDTO;
}

// ---- Chat attachments --------------------------------------------------
// Same multipart shape as previewBackup/restoreBackup above: a File goes
// through FormData, never JSON, so this can't go through `api.*` either --
// no `content-type` header is set, letting the browser fill in the
// multipart boundary itself.

export interface FileAttachmentDTO {
  id: string;
  filename: string;
  contentType: string;
  sizeBytes: number;
  isImage: boolean;
  createdAt: string;
}

/** Mirrors `_MAX_BYTES` in `backend/src/oc8/api/v1/files.py`. The server's
 * check is the authoritative one -- this exists so a 200 MB video fails in
 * the same instant it is picked, instead of after uploading all of it just
 * to be told 413. Never treat it as a security boundary. */
export const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024;

export async function uploadChatAttachment(
  sessionId: string,
  file: File,
): Promise<FileAttachmentDTO> {
  const token = await getToken();
  const body = new FormData();
  body.append("file", file);
  const res = await fetch(`${API_URL}/chat/sessions/${sessionId}/attachments`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}` },
    body,
  });
  if (!res.ok) throw await toError(res);
  return (await res.json()) as FileAttachmentDTO;
}

// Same shape as uploadChatAttachment above, but for the agent Instructions
// tab's own "Attached files" section (owner_type="agent_instructions" on the
// backend rather than "chat_message").
export async function uploadInstructionFile(
  agentId: string,
  file: File,
): Promise<FileAttachmentDTO> {
  const token = await getToken();
  const body = new FormData();
  body.append("file", file);
  const res = await fetch(`${API_URL}/agents/${agentId}/instruction-files`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}` },
    body,
  });
  if (!res.ok) throw await toError(res);
  return (await res.json()) as FileAttachmentDTO;
}

// A plain JSON GET, unlike the multipart calls above -- goes through the
// normal `api.get` helper.
export function listInstructionFiles(agentId: string): Promise<FileAttachmentDTO[]> {
  return api.get<FileAttachmentDTO[]>(`/agents/${agentId}/instruction-files`);
}

// Owner-type-agnostic: `DELETE /files/{id}` (Task 6) works on either a chat
// attachment or an instruction file, so this one function serves both --
// only the Instructions tab actually calls it yet.
export function deleteFile(fileId: string): Promise<void> {
  return api.delete<void>(`/files/${fileId}`);
}

// ---- Capa export -------------------------------------------------------
// Same shape as backup export/preview above: `POST /capas/export` returns
// either a JSON preview (dry_run) or a binary ZIP, so it can't go through
// `api.*`. `CapaExportPreviewItem`/`CapaExportPreviewResult` mirror
// `oc8.api.v1.capas`'s `CapaExportPreviewItem`/`CapaExportPreviewResponse` --
// those are plain `BaseModel`, not `CamelModel`, so the wire (and these
// types) stay snake_case on purpose, same as the backup DTOs above.

export interface CapaExportItemInput {
  kind: "department" | "agent" | "skill";
  id: string;
  name: string;
  version: string;
  summary: string;
}

export interface CapaExportPreviewItem {
  folder_name: string;
  manifest_toml: string;
  warnings: string[];
  extra_files: Record<string, string>;
}

export interface CapaExportPreviewResult {
  items: CapaExportPreviewItem[];
  errors: string[];
}

export async function previewCapaExport(
  items: CapaExportItemInput[],
): Promise<CapaExportPreviewResult> {
  const token = await getToken();
  const res = await fetch(`${API_URL}/capas/export`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
    body: JSON.stringify({ items, dry_run: true }),
  });
  if (!res.ok) throw await toError(res);
  return (await res.json()) as CapaExportPreviewResult;
}

export async function downloadCapaExport(
  items: CapaExportItemInput[],
): Promise<{ blob: Blob; filename: string }> {
  const token = await getToken();
  const res = await fetch(`${API_URL}/capas/export`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
    body: JSON.stringify({ items, dry_run: false }),
  });
  if (!res.ok) throw await toError(res);
  const disposition = res.headers.get("content-disposition") ?? "";
  const match = /filename="?([^";]+)"?/i.exec(disposition);
  return { blob: await res.blob(), filename: match?.[1] ?? "capa-export.zip" };
}

export { API_URL, getToken };
