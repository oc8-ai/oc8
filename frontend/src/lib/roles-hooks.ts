// The tenant's own roles (§6 of the tenant-defined-roles design), reading and
// writing side. Its own module rather than an addition to `governance-hooks.ts`,
// because the two endpoints have opposite promises and mixing them would hide
// that: `GET /governance` is ungated and answers about the CALLER only, while
// everything here is behind `role:view` / `role:manage` / `member:manage` and
// answers about the whole tenant.
//
// Every mutation below invalidates three keys, and the third is the one that is
// easy to forget. `["roles"]` refreshes the list the admin is looking at,
// `["governance"]` refreshes his own resolved permissions -- which the sidebar
// and every `useMay()` read -- and `["auth","me"]` refreshes the seats and flags
// `/me` carries. Without the second, an administrator revokes a role, the API
// starts answering 403 on the very next request, and the sidebar goes on
// offering the links until the query goes stale.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { toQueryString, type ListQueryParams, type Page } from "@/lib/hooks";

/** One right, in words, with its refusal. The refused ones are RETURNED rather
 *  than filtered out: a hidden control produces a support ticket asking where
 *  the setting went, a disabled one with a sentence beside it answers the
 *  question on the screen where it was asked. */
export interface PermissionInfo {
  permission: string;
  /** English -- oc8's default. `labelDe` / `descriptionDe` carry the German;
   *  the screen picks by language rather than the backend picking for it, so
   *  one response serves both and a language switch needs no refetch. */
  label: string;
  description: string;
  labelDe: string;
  descriptionDe: string;
  delegatable: boolean;
  /** Why a tenant-defined role may not hold it. Empty when it may. */
  reason: string;
}

export interface RoleHolder {
  memberId: string;
  subject: string;
  displayName: string;
}

export interface RoleSummary {
  /** Null for a built-in role this tenant has no row for. Both spellings exist
   *  in the wild -- provisioning writes five rows, Globex has one -- and the
   *  ladder is identical either way because it is compiled in. */
  id: string | null;
  name: string;
  description: string;
  kind: string;
  builtin: boolean;
  canEdit: boolean;
  holderCount: number;
  permissions: string[];
}

export interface RoleDetail extends RoleSummary {
  holders: RoleHolder[];
}

const ROLES_KEY = ["roles"] as const;

/** Everything a role or assignment write can change about the CALLER, not just
 *  about the list. See the module header. */
function useAuthorityInvalidator(): () => Promise<void> {
  const qc = useQueryClient();
  return async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ROLES_KEY }),
      qc.invalidateQueries({ queryKey: ["governance"] }),
      qc.invalidateQueries({ queryKey: ["auth", "me"] }),
      // The people list carries each person's CURRENT role now, so an assignment
      // makes it stale in the one place an administrator reads it -- the picker
      // he is standing in front of. Prefix key, so `["members", limit]` matches.
      qc.invalidateQueries({ queryKey: ["members"] }),
    ]);
  };
}

/** The compiled-in catalogue: all 52 permissions with a label and a sentence.
 *
 * Ungated and tenant-free, so it is the one query on this screen that is safe
 * to hold for a long time -- it can only change when the backend is deployed.
 */
export function usePermissionCatalogue() {
  return useQuery({
    queryKey: ["permissions", "catalogue"],
    queryFn: () => api.get<PermissionInfo[]>("/permissions/catalogue"),
    staleTime: 60 * 60 * 1000,
  });
}

/** The built-in ladder plus this tenant's own roles.
 *
 * `enabled` is the caller's `role:view`. Behind a permission on purpose: this is
 * the tenant's authority MAP -- which roles exist and how many people hold them
 * -- and asking for it without the right is a guaranteed 403 rendered as a red
 * box on the one screen whose job is to explain refusals calmly.
 */
export function useRoles(enabled = true) {
  return useQuery({
    queryKey: ROLES_KEY,
    queryFn: () => api.get<RoleSummary[]>("/roles"),
    enabled,
  });
}

/** One role, its grants and everybody holding it -- the blast radius of an edit.
 *
 * A role screen that shows checkboxes and no names asks an administrator to
 * click Save on a number he cannot see.
 */
export function useRole(roleId: string | null) {
  return useQuery({
    queryKey: ["roles", roleId] as const,
    queryFn: () => api.get<RoleDetail>(`/roles/${roleId}`),
    enabled: !!roleId,
  });
}

/** One person standing in one department -- mirrors backend `SeatDTO`. */
export interface MemberSeat {
  departmentId: string;
  departmentName: string;
  /** "dept_viewer" | "dept_approver". */
  seatRole: string;
  agentManage: boolean;
}

/** One person this tenant has seen. The backend sends the full `MemberDTO` on
 *  the wire (seats, allDepartments, firstSeenAt included) whether the caller
 *  is the role picker or the user detail page -- this type carries all of it
 *  so both can read from the same `GET /members` response. */
export interface RoleAssignee {
  id: string;
  subject: string;
  displayName: string;
  /** What they hold TODAY. `null` is the token floor and is not "no
   *  permissions": the picker has to be able to say "Anna — Abteilungsleitung"
   *  before it replaces it, because assigning is a replacement and the previous
   *  role only ever appeared in an audit event no screen renders. */
  roleId?: string | null;
  roleName?: string;
  /** Sees and decides in every department, including ones created later. */
  allDepartments?: boolean;
  firstSeenAt?: string;
  seats?: MemberSeat[];
}

/** How many people the picker asks for. `MAX_LIMIT` on `GET /members`, and it is
 *  passed EXPLICITLY.
 *
 *  It used to send no limit at all and take the backend's default of 200,
 *  ordered by `created_at` -- so on the five-hundred-person tenant this feature
 *  is designed for, the "Assign to…" list held the two hundred OLDEST people and
 *  every joiner after the two hundredth was unreachable through the product,
 *  with no notice anywhere that the list had been cut. */
export const ASSIGNEE_LIMIT = 1000;

/** Everybody the tenant has seen, for the assignment picker.
 *
 * Behind `member:view`, so `enabled` is the caller's own right: an admin who may
 * compose roles but not hand them out sees the editor and no picker, which is
 * exactly what those two permissions mean apart.
 *
 * The ceiling is real and the screen SAYS SO when it is hit: `MAX_LIMIT` on the
 * route is 1000, and a tenant past that is a tenant whose onboarding belongs to
 * `oc8 member import --csv`, which is the bridge the design names. A truncated
 * dropdown that looks complete is the version of that with the sentence removed.
 *
 * `/members` returns the shared `Page[MemberDTO]` envelope like every other
 * list-query endpoint (Design System Consistency plan) -- `.data.items` is the
 * array callers want; `.data.totalCount` is the true tenant-wide count, which
 * can exceed `ASSIGNEE_LIMIT` and is how a caller detects the truncation above.
 * `params` lets a caller override search/pagination while `pageSize` still
 * defaults to `ASSIGNEE_LIMIT` unless the caller supplies its own.
 */
export function useAssignees(enabled = true, params: ListQueryParams = {}) {
  return useQuery({
    queryKey: ["members", ASSIGNEE_LIMIT, params] as const,
    // `/members` aliases `groupBy` like `/agents`/`/departments` -- default
    // `groupByKey` in `toQueryString` is already correct here.
    queryFn: () =>
      api.get<Page<RoleAssignee>>(
        `/members?${toQueryString({ pageSize: ASSIGNEE_LIMIT, ...params })}`,
      ),
    enabled,
  });
}

export function useCreateRole() {
  const invalidate = useAuthorityInvalidator();
  return useMutation({
    mutationFn: (body: {
      name: string;
      description: string;
      permissions: string[];
      // Pre-ticks the boxes from a built-in preset. Sent so the audit event can
      // record where the set came from; the backend deliberately does not store
      // it, because a stored parent is inheritance.
      basedOn?: string | null;
    }) => api.post<RoleDetail>("/roles", body),
    onSuccess: invalidate,
  });
}

export function useUpdateRole() {
  const invalidate = useAuthorityInvalidator();
  return useMutation({
    mutationFn: ({
      roleId,
      ...body
    }: {
      roleId: string;
      description: string;
      permissions: string[];
    }) => api.put<RoleDetail>(`/roles/${roleId}`, body),
    onSuccess: invalidate,
  });
}

/** Soft-delete, refusing with a 409 naming the holders unless `reassignTo` says
 *  where they go. Deleting and letting the holders fall back would restore every
 *  one of them to whatever their TOKEN says, which on every live tenant is
 *  `org_admin` -- a cleanup that silently re-promotes forty people. */
export function useDeleteRole() {
  const invalidate = useAuthorityInvalidator();
  return useMutation({
    mutationFn: ({ roleId, reassignTo }: { roleId: string; reassignTo?: string | null }) =>
      api.delete<RoleDetail>(
        reassignTo
          ? `/roles/${roleId}?reassignTo=${encodeURIComponent(reassignTo)}`
          : `/roles/${roleId}`,
      ),
    onSuccess: invalidate,
  });
}

/** Enrol a person this tenant has not seen yet, by the subject string their
 *  IdP token will carry (their sign-in email, in practice). An upsert on
 *  `(tenant_id, subject)` on the backend, so creating somebody who has
 *  already signed in just adopts their existing row rather than erroring --
 *  which is also how the user detail page reuses this to save a display name
 *  or flip `allDepartments` on somebody who already exists. `displayName`
 *  only ever WIDENS: the backend applies it only when non-empty, so a call
 *  that omits it (e.g. an `allDepartments`-only save) cannot blank out a name
 *  set earlier. `allDepartments` stays tri-state -- omitted leaves it alone. */
/** The two invite fields `POST /members` adds on top of `RoleAssignee`
 *  (backend's `MemberDTO.inviteLink`/`inviteSent`) -- set only when the
 *  request omitted a password, community mode is active, and the member had
 *  no password already. Widened here rather than added to `RoleAssignee`
 *  itself: every OTHER consumer of that type reads a plain member row that
 *  never carries either field. */
export type CreatedMember = RoleAssignee & {
  inviteLink?: string | null;
  inviteSent?: boolean;
};

export function useCreateMember() {
  const invalidate = useAuthorityInvalidator();
  return useMutation({
    mutationFn: (body: {
      subject: string;
      displayName?: string;
      allDepartments?: boolean;
      password?: string;
    }) =>
      api.post<CreatedMember>("/members", {
        subject: body.subject,
        displayName: body.displayName ?? "",
        ...(body.allDepartments === undefined ? {} : { allDepartments: body.allDepartments }),
        ...(body.password ? { password: body.password } : {}),
      }),
    onSuccess: invalidate,
  });
}

/** Set or reset a member's local password. Only meaningful in Community's
 *  local-password auth mode (see `useAuthConfig`) -- always callable, since
 *  `POST /auth/login` checks `password_hash` alone, but the login screen
 *  only offers a password FORM in that mode. Never returns the password or
 *  its hash; success is a 204. */
export function useSetMemberPassword() {
  return useMutation({
    mutationFn: ({ memberId, password }: { memberId: string; password: string }) =>
      api.put<void>(`/members/${memberId}/password`, { password }),
  });
}

/** What `POST /members/{id}/password-reset` returns: the link is always
 *  present so a missing mail server is not a dead end; `resetSent` is
 *  whether `deliver` actually handed it to the relay. */
export interface MemberPasswordReset {
  resetLink: string;
  resetSent: boolean;
}

/** Mint a fresh set-password link for an existing member and try to email
 *  it. The user-detail counterpart to the invite `POST /members` mints on
 *  create -- an administrator looking at somebody who already exists had
 *  no other door. */
export function useMintMemberPasswordReset() {
  return useMutation({
    mutationFn: (memberId: string) =>
      api.post<MemberPasswordReset>(`/members/${memberId}/password-reset`),
  });
}

/** Soft-delete a person so they disappear from the users list. Refused
 *  with a 409 if the caller is looking at themselves -- same lockout
 *  `PUT /members/{id}/role` already refuses, by a different verb. */
export function useDeleteMember() {
  const invalidate = useAuthorityInvalidator();
  return useMutation({
    mutationFn: (memberId: string) => api.delete<void>(`/members/${memberId}`),
    onSuccess: invalidate,
  });
}

/** Rename a member's sign-in identity (their email, in local-password mode).
 *  Refused with a 409 if another live member already holds the target
 *  identity -- surfaced to the caller as a thrown error, same as every other
 *  mutation here. */
export function useRenameMemberSubject() {
  const invalidate = useAuthorityInvalidator();
  return useMutation({
    mutationFn: ({ memberId, subject }: { memberId: string; subject: string }) =>
      api.put<RoleAssignee>(`/members/${memberId}/subject`, { subject }),
    onSuccess: invalidate,
  });
}

/** Seat somebody in one department, at one of exactly two seat roles
 *  (`dept_viewer` | `dept_approver`), with independent write authority over
 *  that department's agents. Also the way to CHANGE an existing seat -- the
 *  backend route is an upsert on `(member_id, department_id)`. */
export function useGrantSeat() {
  const invalidate = useAuthorityInvalidator();
  return useMutation({
    mutationFn: ({
      memberId,
      departmentId,
      seatRole,
      agentManage,
    }: {
      memberId: string;
      departmentId: string;
      seatRole: string;
      agentManage: boolean;
    }) =>
      api.put<RoleAssignee>(`/members/${memberId}/departments/${departmentId}`, {
        seatRole,
        agentManage,
      }),
    onSuccess: invalidate,
  });
}

/** Take a seat away. Effective on that person's next request. */
export function useRevokeSeat() {
  const invalidate = useAuthorityInvalidator();
  return useMutation({
    mutationFn: ({ memberId, departmentId }: { memberId: string; departmentId: string }) =>
      api.delete<RoleAssignee>(`/members/${memberId}/departments/${departmentId}`),
    onSuccess: invalidate,
  });
}

/** Give one person a role, or take the override away.
 *
 * `roleId: null` is NOT "no permissions": it clears the override and the person
 * resolves through their token exactly as before this feature existed. It is the
 * documented repair for a demotion, and the label on the button says so.
 */
export function useAssignRole() {
  const invalidate = useAuthorityInvalidator();
  return useMutation({
    mutationFn: ({ memberId, roleId }: { memberId: string; roleId: string | null }) =>
      api.put<RoleDetail | null>(`/members/${memberId}/role`, { roleId }),
    onSuccess: invalidate,
  });
}

/** The same role for many people, in one transaction, one audit event each. */
export function useBulkAssignRole() {
  const invalidate = useAuthorityInvalidator();
  return useMutation({
    mutationFn: ({ memberIds, roleId }: { memberIds: string[]; roleId: string | null }) =>
      api.post<RoleHolder[]>("/members/roles:bulk", { memberIds, roleId }),
    onSuccess: invalidate,
  });
}
