// Hooks for the governance screen (§5.2 / §17.2-6, reading side). Its own
// module rather than an addition to hooks.ts, matching audit-hooks.ts and
// knowledge-connector-hooks.ts.

import { useCallback } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import type { Seat } from "./hooks";

export interface GovernanceRole {
  name: string;
  builtin: boolean;
  permissions: string[];
  /** `human` | `agent`. On the wire because the two lists below are the same
   * shape, and a client that merged them would silently recreate the conflation
   * this slice removed — `agent_default`, whose whole content is
   * `tool:read|write|send`, used to be the leftmost column of this screen. */
  kind?: string;
  /** Always null here: everything this endpoint lists is compiled in, so there
   * is no row to address. `GET /roles` returns the same names WITH ids. */
  id?: string | null;
}

export interface Governance {
  /** Every permission the system knows. Resources are derived from these by
   * splitting on the colon, so the screen never carries a second list that can
   * fall out of step with the backend's. */
  permissions: string[];
  /** The built-in roles a PERSON can be. */
  roles: GovernanceRole[];
  /** The built-in roles an AGENT can be — a separate list, not a flag on the one
   * above. They describe what a program may do in a customer's system,
   * intersected with its department frame and its own narrowing, and nobody can
   * be given one. Optional so an older backend renders as it did. */
  agentRoles?: GovernanceRole[];
  /** Exactly what the caller's token says, NOT normalised to a known role. An
   * unmapped role has to be visible as itself — "your token says `menber`" is
   * the answer; "unknown" is not. */
  callerRole: string;
  /** What the caller ACTUALLY holds, RESOLVED: the token's role, or the role an
   * administrator assigned them. Never the token's set on its own — a demoted
   * administrator's own diagnostic screen would otherwise tell him he holds all
   * 52 while every gate refuses him. */
  callerPermissions: string[];
  /** The capabilities the caller's organisation holds, or null when the
   * deployment does not resolve entitlements per organisation (unknown). */
  callerCapabilities?: string[] | null;
  callerRoleIsKnown: boolean;
  /** `token` when nobody has assigned them anything — which is every caller on
   * every tenant that has configured nothing — `assigned` when a row decides.
   * The two produce different sentences, and only one of them tells somebody who
   * to go and ask. */
  callerRoleSource?: "token" | "assigned";
  /** The name of the assigned role, null when the token decides. This is the
   * word an employee repeats to their administrator. */
  callerTenantRoleName?: string | null;
  /** `human` for a person. Anything else is an assignment pointing at a row a
   * person can never resolve, and is worth showing as itself rather than as
   * "this role grants nothing". */
  callerRoleKind?: string;
  /** True when a role WAS assigned and the row cannot grant — soft-deleted, or
   * an agent-kind row. The holder has nothing tenant-wide, and the reason is the
   * assignment rather than anything about their token.
   *
   * It is a field of its own because it cannot be inferred here: the backend
   * deliberately sends no `callerTenantRoleName` for an unusable assignment (a
   * name beside an empty set reads as "the role is empty", not "the role is
   * broken"), so from the client's side this state is indistinguishable from a
   * plain token — and it was being rendered as one, in the state that is the
   * only genuine misconfiguration of the three. */
  callerRoleUnusable?: boolean;
  /** True when the database could not be read. The static model is still there
   * and the caller's own half is missing and says so. The screen that explains
   * refusals is the worst page in the product to lose during an incident. */
  authorityUnavailable?: boolean;
  /** The closed four-permission seat vocabulary, keyed by seat role. Sent so the
   * screen explaining a refusal can say what a departmental grant means without
   * carrying the two seat-role names in its own source. */
  seatRoles?: Record<string, string[]>;
  /** The caller's OWN live seats. Deliberately NOT folded into
   * `callerPermissions`: a seat is authority somewhere, and a flat list of
   * permissions cannot say where. */
  seats?: Seat[];
}

export function useGovernance() {
  return useQuery<Governance>({
    queryKey: ["governance"],
    queryFn: () => api.get<Governance>("/governance"),
    // This used to hold for five minutes, on the grounds that "the model is
    // defined in code, so it cannot change while a screen is open". That is now
    // false for half the payload: `callerPermissions` is RESOLVED from a row an
    // administrator can rewrite at any moment, and the API acts on the change on
    // the caller's very next request. Held for five minutes, a revoked role
    // would leave the sidebar offering links that 403 — the revocation would
    // look like a bug rather than like a revocation. Thirty seconds is the
    // backstop; the mutations in `roles-hooks.ts` invalidate this key outright,
    // which is what actually makes a change land.
    staleTime: 30 * 1000,
  });
}

/** "May the caller's ROLE do this, tenant-wide?"
 *
 * Backed by `callerPermissions`, which is exactly what the role grants and
 * deliberately says nothing about seats. So this answers the nav's question --
 * "would this screen 403 him" -- and NOT the workspace's, whose authority is a
 * seat and which therefore must never be hidden behind it. An employee's role is
 * `member`, which holds the empty set, so every screen backed by a tenant-wide
 * permission disappears from his sidebar and the two that do not stay.
 *
 * While the query is unresolved it answers TRUE for everything, on purpose: the
 * alternative is an empty sidebar on every first paint for every administrator,
 * and a nav item that 403s once is a smaller lie than a nav that is not there.
 * A failed governance fetch lands in the same branch, which is exactly today's
 * behaviour rather than a new lockout.
 */
export function useCan(): (permission: string) => boolean {
  const { data } = useGovernance();
  const granted = data?.callerPermissions;
  return useCallback(
    (permission: string) => (granted ? granted.includes(permission) : true),
    [granted],
  );
}

/** "May the caller do this?" — answered STRICTLY, for controls that WRITE.
 *
 * The difference from `useCan` is the unresolved state, and it is deliberate
 * rather than an oversight in one of them. `useCan` answers TRUE before the
 * model has landed, because the alternative is an empty sidebar on every first
 * paint for every administrator. This answers FALSE, because the alternative is
 * a Delete button that appears for a second and then 403s — and because that is
 * exactly the "false while pending/errored → fail-safe hide" contract the
 * deleted `useIsAdmin` documented, which every one of its call sites was written
 * against.
 *
 * `useIsAdmin` is gone precisely because it never asked this question: it
 * compared the token's role string to `"org_admin"`, which is the one place the
 * frontend bypassed the permission model. Any assigned role, however privileged,
 * silently lost whatever it gated — including an assigned `org_admin`.
 */
export function useMay(): (permission: string) => boolean {
  const { data } = useGovernance();
  const granted = data?.callerPermissions;
  return useCallback(
    (permission: string) => (granted ? granted.includes(permission) : false),
    [granted],
  );
}

/** "May the caller write to THIS agent's department?" — the department-scoped
 * counterpart to `useMay()('agent:manage')`, which only ever answers for a
 * TENANT-WIDE grant.
 *
 * Two independent grants reconciled in one place, matching the backend's
 * `authorize_agent_write`:
 *   1. tenant-wide `agent:manage`, from `callerPermissions` — admits
 *      everywhere, `departmentId` unconsulted (including `null`/`undefined`).
 *   2. the seat-native `agentManage` toggle, carried on `Governance.seats`
 *      and NEVER folded into `callerPermissions` — a seat is authority
 *      SOMEWHERE, and a flat permission list cannot say where, so it can only
 *      admit a `departmentId` that matches one of the caller's own live seats.
 *
 * Fails closed while `useGovernance` is pending or has errored, like
 * `useMay()` and unlike `useCan()`: the callers here are write controls
 * (Start/Pause/Stop, Save, Assign skill, the model-config select), and a
 * button that is live for a moment and then 403s is the exact failure mode
 * `useMay()`'s own doc comment exists to prevent.
 */
export function useMayManageAgent(): (departmentId: string | null | undefined) => boolean {
  const { data } = useGovernance();
  return useCallback(
    (departmentId: string | null | undefined) => {
      if (!data) return false;
      if (data.callerPermissions.includes("agent:manage")) return true;
      if (!departmentId) return false;
      return !!data.seats?.some((s) => s.departmentId === departmentId && s.agentManage);
    },
    [data],
  );
}

/** What the caller's role is CALLED, resolved — the name to put under their own
 *  avatar and nowhere else.
 *
 * Returns the assigned role's own name when an administrator has assigned one,
 * and `null` when the token decides, in which case the caller should fall back
 * to translating the token's built-in role name.
 *
 * This exists because the sidebar read `MeDTO.role`, which is `principal.role` —
 * the TOKEN — so Anna, demoted to *Freigabe Vertrieb*, was told "Administrator"
 * under her name on every page while `/governance` two clicks away named her real
 * role and every gate refused her. That is the exact comparison `useIsAdmin()` was
 * deleted for, left standing one file away.
 *
 * `null` while the query is in flight, so nothing renders a name it does not have
 * yet; the caller's existing token-based label is the right thing to show then,
 * because for every caller on every tenant that has configured nothing it is also
 * the right answer afterwards.
 */
export function useAssignedRoleName(): string | null {
  const { data } = useGovernance();
  if (!data || data.callerRoleSource !== "assigned") return null;
  return data.callerTenantRoleName ?? null;
}

/** Group permissions by the resource before the colon, preserving sort order.
 * Exported so the screen and its tests agree on the grouping. */
export function byResource(permissions: string[]): Map<string, string[]> {
  const out = new Map<string, string[]>();
  for (const permission of [...permissions].sort()) {
    const [resource] = permission.split(":", 1);
    const list = out.get(resource) ?? [];
    list.push(permission);
    out.set(resource, list);
  }
  return out;
}
