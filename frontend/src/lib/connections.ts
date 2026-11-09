// Connections (personal and organisation accounts at other services): types
// and hooks over /connections/*, /oauth/* and /agents/{id}/tool-access.
// Server data only -- no component state lives here, so a background
// refetch can never close a dialog or drop a banner.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";

export type IntegrationKind = "oauth" | "credential";
export type OAuthProviderId = "microsoft" | "google";
export type Owner = "me" | "organisation";

export interface CredentialField {
  key: string;
  label: string;
  kind: string;
  required?: boolean;
  default?: string;
  placeholder?: string;
  help?: string;
  label_translations?: Record<string, string>;
  help_translations?: Record<string, string>;
  placeholder_translations?: Record<string, string>;
}

export interface Integration {
  name: string;
  label: string;
  summary: string;
  toolKey: string;
  kind: IntegrationKind;
  provider: OAuthProviderId | null;
  credentialType: string | null;
  credentialFields: CredentialField[];
  available: boolean;
  capaId: string;
  /** The plugin's requested permissions, for "make available" (Task 22). */
  permissions?: string[];
}

export interface Connection {
  id: string;
  kind: IntegrationKind;
  integration: string | null;
  provider: string | null;
  credentialType: string | null;
  accountLabel: string;
  owner: "me" | "organisation" | "member";
  ownerMemberId: string | null;
  ownerName: string | null;
  /** OAuth: active | needs_reauth | revoked. Credential: ok | failed | untested. */
  status: string;
  grantType: string | null;
  createdAt: string;
  createdByName: string | null;
  lastTestedAt: string | null;
  lastTestOk: boolean | null;
}

export interface CredentialCheck {
  ok: boolean;
  reason: "auth" | "unreachable" | "invalid" | null;
  detail: string | null;
}

export interface ConnectionRequiredInfo {
  integration: string;
  provider: string | null;
  label: string;
  reason:
    | "not_connected"
    | "needs_reauth"
    | "no_acting_person"
    | "schedule_without_owner"
    | "schedule_needs_confirmation"
    | "trigger_without_person"
    | "binding_needs_confirmation"
    | "not_configured";
}

export interface AgentToolAccess {
  toolKey: string;
  integration: string;
  label: string;
  kind: IntegrationKind;
  mode: "default" | "user" | "fixed";
  defaultMode: "user" | "organisation";
  oauthConnectionId: string | null;
  credentialId: string | null;
  targetLabel: string | null;
  targetOwnerName: string | null;
  targetMissing: boolean;
  /** A fixed binding runs refuse (`binding_needs_confirmation`) until its
   *  owner or an administrator saves it again. */
  needsConfirmation: boolean;
  confirmationReason: "not_confirmed" | "agent_changed" | null;
}

export interface AccessOption {
  kind: "user" | "oauth" | "credential";
  id: string | null;
  label: string;
  owner: "organisation" | "me" | "member" | null;
  ownerName: string | null;
}

/** Codes of `POST /oauth/{provider}/complete`'s typed failures, plus the
 *  provider-returned ones the callback redirect carries in `?error=`. */
export const OAUTH_ERROR_CODES = [
  "admin_consent_required",
  "tenant_mismatch",
  "account_unresolved",
  "not_permitted",
  "account_conflict",
  "exchange_failed",
  "consent_required",
  "access_denied",
  "interaction_required",
  "provider_error",
] as const;
export type OAuthErrorCode = (typeof OAUTH_ERROR_CODES)[number];

function isOAuthErrorCode(v: unknown): v is OAuthErrorCode {
  return typeof v === "string" && (OAUTH_ERROR_CODES as readonly string[]).includes(v);
}

export class OAuthCompleteError extends Error {
  code: OAuthErrorCode;
  /** Microsoft only: where an administrator grants consent. */
  adminConsentUrl: string | null;
  constructor(code: OAuthErrorCode, adminConsentUrl: string | null = null) {
    super(code);
    this.name = "OAuthCompleteError";
    this.code = code;
    this.adminConsentUrl = adminConsentUrl;
  }
}

export const connectionKeys = {
  all: ["connections"] as const,
  integrations: ["connections", "integrations"] as const,
  mine: ["connections", "mine"] as const,
  organisation: ["connections", "organisation"] as const,
  members: ["connections", "members"] as const,
};

export type ConnectionState = "unavailable" | "not_connected" | "connected" | "needs_reauth";

/** The caller's own state for one integration. Organisation and other
 *  members' rows never count: a person is "connected" only with their own. */
export function connectionStateOf(
  integration: Integration | undefined,
  mine: readonly Connection[],
): { state: ConnectionState; connections: Connection[] } {
  if (!integration || !integration.available) return { state: "unavailable", connections: [] };
  const own = mine.filter((c) => c.integration === integration.name && c.owner === "me");
  if (own.length === 0) return { state: "not_connected", connections: [] };
  const healthy = own.some((c) =>
    c.kind === "oauth" ? c.status === "active" : c.status !== "failed",
  );
  return { state: healthy ? "connected" : "needs_reauth", connections: own };
}

export function useIntegrations() {
  return useQuery({
    queryKey: connectionKeys.integrations,
    queryFn: () => api.get<Integration[]>("/connections/integrations"),
  });
}

export function useMyConnections() {
  return useQuery({
    queryKey: connectionKeys.mine,
    queryFn: () => api.get<Connection[]>("/connections/mine"),
  });
}

export function useOrganisationConnections(enabled: boolean) {
  return useQuery({
    queryKey: connectionKeys.organisation,
    queryFn: () => api.get<Connection[]>("/connections/organisation"),
    enabled,
  });
}

export function useMemberConnections(enabled: boolean) {
  return useQuery({
    queryKey: connectionKeys.members,
    queryFn: () => api.get<Connection[]>("/connections/members"),
    enabled,
  });
}

/** Connections changed: refresh them and every agent's tool-access data
 *  (bindings, their labels and the pickable options depend on them). */
function useInvalidateConnections() {
  const qc = useQueryClient();
  return () =>
    Promise.all([
      qc.invalidateQueries({ queryKey: connectionKeys.all }),
      qc.invalidateQueries({
        queryKey: ["agents"],
        predicate: (q) => q.queryKey[2] === "tool-access",
      }),
    ]);
}

export function useStartOAuth() {
  return useMutation({
    mutationFn: ({ provider, owner }: { provider: OAuthProviderId; owner: Owner }) =>
      api.post<{ authorizationUrl: string }>(`/oauth/${provider}/start`, {
        scopeSet: "tools",
        owner,
      }),
  });
}

/** Finishes the OAuth round trip with the `pending` id the callback redirect
 *  carried. A typed failure rejects with an `OAuthCompleteError`. */
export function useCompleteOAuth() {
  const invalidate = useInvalidateConnections();
  return useMutation({
    mutationFn: async ({ provider, pending }: { provider: OAuthProviderId; pending: string }) => {
      try {
        const out = await api.post<{ connected: string; connection: Connection }>(
          `/oauth/${provider}/complete`,
          {
            pending,
          },
        );
        return out.connection;
      } catch (err) {
        const body = (err instanceof ApiError ? err.body : undefined) as
          | { error?: unknown; adminConsentUrl?: unknown }
          | undefined;
        if (isOAuthErrorCode(body?.error)) {
          throw new OAuthCompleteError(
            body.error,
            typeof body.adminConsentUrl === "string" ? body.adminConsentUrl : null,
          );
        }
        throw err;
      }
    },
    onSuccess: invalidate,
  });
}

export function useDisconnect() {
  const invalidate = useInvalidateConnections();
  return useMutation({
    mutationFn: (c: Pick<Connection, "id" | "kind">) =>
      api.delete(
        c.kind === "oauth" ? `/connections/oauth/${c.id}` : `/connections/credentials/${c.id}`,
      ),
    onSuccess: invalidate,
  });
}

export function useCheckCredential() {
  return useMutation({
    mutationFn: (body: { integration: string; fieldValues: Record<string, string> }) =>
      api.post<CredentialCheck>("/connections/credentials/check", body),
  });
}

export function useCreateCredentialConnection() {
  const invalidate = useInvalidateConnections();
  return useMutation({
    mutationFn: (body: {
      integration: string;
      owner: Owner;
      fieldValues: Record<string, string>;
    }) => api.post<Connection>("/connections/credentials", body),
    onSuccess: invalidate,
  });
}

/** An organisation access's values ("Bearbeiten"): the existing admin route. */
export function useUpdateOrganisationCredential() {
  const invalidate = useInvalidateConnections();
  return useMutation({
    mutationFn: ({ id, fieldValues }: { id: string; fieldValues: Record<string, string> }) =>
      api.patch(`/credentials/${id}`, { fieldValues }),
    onSuccess: invalidate,
  });
}

export function useTestConnection() {
  const invalidate = useInvalidateConnections();
  return useMutation({
    mutationFn: (id: string) => api.post<CredentialCheck>(`/connections/credentials/${id}/test`),
    onSuccess: invalidate,
  });
}

export function useAdminConsentUrl(enabled: boolean) {
  return useQuery({
    queryKey: ["oauth", "microsoft", "admin-consent-url"],
    queryFn: () => api.get<{ url: string }>("/oauth/microsoft/admin-consent-url"),
    enabled,
    retry: false,
    staleTime: Number.POSITIVE_INFINITY,
  });
}

export function useConnectionState(name: string) {
  const integrations = useIntegrations();
  const mine = useMyConnections();
  const integration = integrations.data?.find((i) => i.name === name);
  return {
    isLoading: integrations.isLoading || mine.isLoading,
    integration,
    ...connectionStateOf(integration, mine.data ?? []),
  };
}

const accessKey = (agentId: string) => ["agents", agentId, "tool-access"] as const;

export function useAgentToolAccess(agentId: string) {
  return useQuery({
    queryKey: accessKey(agentId),
    queryFn: () => api.get<AgentToolAccess[]>(`/agents/${agentId}/tool-access`),
    enabled: !!agentId,
  });
}

export function useAgentToolAccessOptions(agentId: string, toolKey: string, enabled: boolean) {
  return useQuery({
    queryKey: [...accessKey(agentId), toolKey, "options"],
    queryFn: () => api.get<AccessOption[]>(`/agents/${agentId}/tool-access/${toolKey}/options`),
    enabled: enabled && !!agentId,
  });
}

export function useSetAgentToolAccess(agentId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      toolKey,
      body,
    }: {
      toolKey: string;
      body: {
        mode: "user" | "fixed";
        oauthConnectionId?: string;
        credentialId?: string;
        confirmShare?: boolean;
      };
    }) => api.put<AgentToolAccess>(`/agents/${agentId}/tool-access/${toolKey}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: accessKey(agentId) }),
  });
}

export function useResetAgentToolAccess(agentId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (toolKey: string) => api.delete(`/agents/${agentId}/tool-access/${toolKey}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: accessKey(agentId) }),
  });
}

/** A tool call the department frame (or the agent's own guardrails)
 *  withholds (backend oc8.connections.tool_denied): which tool, and whose
 *  guardrails allow it. */
export interface ToolDeniedInfo {
  connection: string;
  tool: string | null;
  label: string | null;
  right: string | null;
  reason:
    | "tool_not_in_frame"
    | "connection_not_in_frame"
    | "connection_disabled"
    | "right_not_granted";
  scope: "department" | "agent";
  departmentId: string | null;
  agentId: string | null;
}
