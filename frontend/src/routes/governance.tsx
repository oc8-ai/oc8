import { createFileRoute } from "@tanstack/react-router";
import {
  AlertTriangle,
  Bot,
  Check,
  ChevronRight,
  Lock,
  MapPin,
  Minus,
  Plus,
  ShieldCheck,
  Trash2,
  UserPlus,
  Users,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { Panel, roleLabel } from "@/components/app-shell";
import {
  byResource,
  useGovernance,
  type Governance,
  type GovernanceRole,
} from "@/lib/governance-hooks";
import {
  ASSIGNEE_LIMIT,
  useAssignees,
  useAssignRole,
  useBulkAssignRole,
  useCreateRole,
  useDeleteRole,
  usePermissionCatalogue,
  useRole,
  useRoles,
  useUpdateRole,
  type PermissionInfo,
  type RoleAssignee,
  type RoleSummary,
} from "@/lib/roles-hooks";
import type { Seat } from "@/lib/hooks";
import { useLang, useT } from "@/lib/i18n";
import { SkeletonRows } from "@/components/skeleton-rows";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

export const Route = createFileRoute("/governance")({
  component: GovernancePage,
});

/** Who may do what — and, for the caller, why they were refused (§5.2, §17.2-6).
 *
 * Four panels, and the order is the order of the questions somebody arrives
 * with. **Your access** answers "why is my screen empty", which nothing else in
 * the product answers and which is the reason `GET /governance` carries no gate
 * at all. **Your own roles** is the editor: a role is a NAME and a SET of
 * permission strings, nothing else, and every right that may not be in one is
 * shown disabled with the sentence saying why — explaining its own refusals is
 * this screen's whole ethos, and a hidden control only produces a support
 * ticket. **In code** and **Agent roles** are the two read-only populations,
 * kept apart because they were conflated: `agent_default`, whose entire content
 * is `tool:read|write|send`, used to be the leftmost column of the matrix below,
 * sitting next to `plugin:manage` as though a person could be handed it.
 *
 * The editor comes SECOND rather than last, which is where the design lists it.
 * It is the only panel on the page with a button, and the nav item above now
 * promises "Zugriff & Rollen": an administrator who came here to compose a role
 * should not have to scroll a 52-row matrix and a table of agent vocabularies
 * to reach the one thing he can act on. */
function GovernancePage() {
  const t = useT();
  const { data, isLoading, error } = useGovernance();

  if (isLoading) {
    return (
      <Panel className="p-5">
        <SkeletonRows
          label={t("Loading the permission model…", "Berechtigungsmodell wird geladen…")}
          rows={4}
          rowClassName="h-10"
        />
      </Panel>
    );
  }

  if (error || !data) {
    return (
      <Panel className="p-8">
        <p className="text-sm text-destructive">
          {t(
            "The permission model could not be loaded.",
            "Das Berechtigungsmodell konnte nicht geladen werden.",
          )}
        </p>
      </Panel>
    );
  }

  return (
    <div className="grid gap-4">
      <YourAccess data={data} />
      <TenantRoles data={data} />
      <BuiltInRoles data={data} />
      <AgentRoles data={data} />
    </div>
  );
}

/* -------------------------------------------------------------- your access */

/** Leads with the caller, because the reason somebody opens this screen is
 * almost always that something refused them.
 *
 * Four states now, and each of them is a different person.
 *
 * 1. **An assigned role decides.** `callerPermissions` is the RESOLVED set, so
 *    what this panel prints is what the gates will actually do. Before that it
 *    read `permissions_for(token.role)`, which for exactly the population this
 *    slice creates would have said "you hold 52 of 52" to somebody being refused
 *    at 48 of them. The token's own role is still printed beside it, because
 *    "which of the two decides" is the question an administrator asks next.
 * 2. **An unrecognised token role** — the amber one, and the only amber one.
 * 3. **A correctly-provisioned employee**, whose token role is `member`: known,
 *    grants nothing company-wide on purpose, holds everything through seats.
 *    Without this state the one panel this layer is proud of told 500 correctly
 *    configured people that their token was broken and pointed their
 *    administrator at an identity-provider remap onto `operator` — i.e. at
 *    exactly the company-wide grant the department boundary exists to avoid.
 * 4. **A built-in role**, unchanged.
 *
 * `authorityUnavailable` sits above all four: the database could not be read,
 * the token half of the answer is still true, and saying so is better than
 * either 500ing or quietly rendering a demotion during an incident. */
function YourAccess({ data }: { data: Governance }) {
  const t = useT();
  const grouped = byResource(data.callerPermissions);
  const seats = data.seats ?? [];
  const seatRoles = data.seatRoles ?? {};
  const source = data.callerRoleSource ?? "token";
  const assignedName = source === "assigned" ? (data.callerTenantRoleName ?? null) : null;
  // A role the deployment defines that simply grants nothing company-wide. It is
  // not a misconfiguration and must not wear the warning triangle.
  const grantsNothingTenantWide = data.callerRoleIsKnown && data.callerPermissions.length === 0;
  const unknown = !assignedName && !data.callerRoleIsKnown;
  // An assignment that cannot grant: the row is soft-deleted, or it is an
  // agent-kind row a person can never hold. Shown as ITSELF, because the
  // alternative was this screen saying "your token carries org_admin, which
  // holds nothing company-wide by design" — false in both halves, and it sends
  // the administrator to look at seats when the fix is one nullable column.
  //
  // Read from the backend's own flag rather than inferred from the name being
  // absent: the name is absent here BY DESIGN (a name beside an empty set reads
  // as "the role is empty" rather than "the role is broken"), so from here this
  // state and a plain token are the same shape.
  const unusableAssignment = data.callerRoleUnusable === true;

  return (
    <Panel className="p-6">
      {data.authorityUnavailable && (
        <div className="mb-4 flex items-start gap-2 rounded-md border border-status-warning/40 bg-status-warning/10 px-3 py-2 text-xs text-status-warning">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
          <span>
            {t(
              "Your assigned role could not be read from the database just now. What follows is what your token carries; an assignment on top of it would not be shown. Nothing was changed.",
              "Ihre zugewiesene Rolle konnte gerade nicht aus der Datenbank gelesen werden. Unten steht, was Ihr Token trägt; eine darüberliegende Zuweisung wird nicht angezeigt. Es wurde nichts geändert.",
            )}
          </span>
        </div>
      )}
      <div className="flex items-start gap-3">
        {unknown || unusableAssignment ? (
          <AlertTriangle className="mt-0.5 size-5 shrink-0 text-status-warning" />
        ) : (
          <ShieldCheck className="mt-0.5 size-5 shrink-0 text-status-running" />
        )}
        <div className="min-w-0 flex-1">
          <h2 className="flex flex-wrap items-center gap-2 font-serif text-xl">
            {t("Your access", "Ihr Zugriff")}
            <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">
              {assignedName ?? data.callerRole}
            </span>
            {assignedName && (
              <span className="rounded border border-border px-1.5 py-0.5 micro-label">
                {t("assigned", "zugewiesen")}
              </span>
            )}
          </h2>

          {unusableAssignment ? (
            <p className="mt-1 text-sm text-status-warning">
              {t(
                `A role was assigned to you, but it can no longer grant anything — it has been deleted, or it is a role for AGENTS, which no person can hold. That, and not the role “${data.callerRole}” in your token, is why you hold nothing company-wide. Ask an administrator to assign you a different role or to clear the assignment.`,
                `Ihnen wurde eine Rolle zugewiesen, die nichts mehr gewähren kann — sie wurde gelöscht, oder sie ist eine Rolle für AGENTEN, die kein Mensch halten kann. Daran liegt es, dass Sie unternehmensweit nichts halten, und nicht an der Rolle „${data.callerRole}“ in Ihrem Token. Bitten Sie eine Administratorin, Ihnen eine andere Rolle zuzuweisen oder die Zuweisung zu löschen.`,
              )}
            </p>
          ) : assignedName ? (
            <p className="mt-1 text-sm text-muted-foreground">
              {data.callerPermissions.length === 0
                ? t(
                    `Your rights come from the role “${assignedName}”, which an administrator assigned to you. It replaces the role “${data.callerRole}” your token carries, and it grants nothing company-wide — your authority is the departments you are seated in, listed below.`,
                    `Ihre Rechte stammen aus der Rolle „${assignedName}“, die Ihnen eine Administratorin zugewiesen hat. Sie ersetzt die Rolle „${data.callerRole}“ aus Ihrem Token und gewährt unternehmensweit nichts — Ihre Befugnis sind die Abteilungen, in denen Sie einen Platz haben, unten aufgeführt.`,
                  )
                : t(
                    `Your rights come from the role “${assignedName}”, which an administrator assigned to you. It replaces the role “${data.callerRole}” your token carries, and it holds ${data.callerPermissions.length} of ${data.permissions.length} permissions.`,
                    `Ihre Rechte stammen aus der Rolle „${assignedName}“, die Ihnen eine Administratorin zugewiesen hat. Sie ersetzt die Rolle „${data.callerRole}“ aus Ihrem Token und hält ${data.callerPermissions.length} von ${data.permissions.length} Berechtigungen.`,
                  )}
            </p>
          ) : unknown ? (
            // The case this screen exists for: an unmapped role holds nothing,
            // so every other screen is empty — which reads as a broken product
            // rather than as a mapping nobody finished.
            <p className="mt-1 text-sm text-status-warning">
              {t(
                `This deployment does not define a role called “${data.callerRole}”, so it grants nothing at all. That is why other screens appear empty. Map your identity provider’s group to one of the roles below.`,
                `Diese Installation kennt keine Rolle „${data.callerRole}“, deshalb gewährt sie gar nichts. Genau darum wirken die übrigen Ansichten leer. Ordnen Sie die Gruppe Ihres Identitätsanbieters einer der unten aufgeführten Rollen zu.`,
              )}
            </p>
          ) : grantsNothingTenantWide ? (
            <p className="mt-1 text-sm text-muted-foreground">
              {t(
                `Your token carries the role “${data.callerRole}”, which holds nothing company-wide by design. Your authority is the departments you are seated in, listed below.`,
                `Ihr Token trägt die Rolle „${data.callerRole}“, die bewusst nichts unternehmensweit gewährt. Ihre Befugnis sind die Abteilungen, in denen Sie einen Platz haben — unten aufgeführt.`,
              )}
            </p>
          ) : (
            <p className="mt-1 text-sm text-muted-foreground">
              {t(
                `Your token carries the role “${data.callerRole}”, which holds ${data.callerPermissions.length} of ${data.permissions.length} permissions. Nobody has assigned you a role of this tenant’s own.`,
                `Ihr Token trägt die Rolle „${data.callerRole}“ und hält ${data.callerPermissions.length} von ${data.permissions.length} Berechtigungen. Eine mandanteneigene Rolle wurde Ihnen nicht zugewiesen.`,
              )}
            </p>
          )}

          {grouped.size > 0 && (
            <div className="mt-4 flex flex-wrap gap-1.5">
              {[...grouped.entries()].map(([resource, permissions]) => (
                <span
                  key={resource}
                  className="rounded border border-border px-2 py-1 text-xs"
                  title={permissions.join(", ")}
                >
                  {resource}
                  <span className="ml-1.5 text-muted-foreground">
                    {permissions.map((p) => p.split(":")[1]).join(" · ")}
                  </span>
                </span>
              ))}
            </div>
          )}

          <YourSeats seats={seats} seatRoles={seatRoles} />
        </div>
      </div>
    </Panel>
  );
}

/** The caller's own seats — deliberately below the role, because a seat is
 * authority SOMEWHERE and the flat permission list above cannot say where.
 *
 * The empty case is not decoration either: it is the answer to "why is my
 * workspace refusing me", and the screen the workspace's empty state links to
 * had no way to give it. */
function YourSeats({ seats, seatRoles }: { seats: Seat[]; seatRoles: Record<string, string[]> }) {
  const t = useT();

  if (seats.length === 0) {
    return (
      <div className="mt-4 rounded-md border border-border bg-muted/20 px-3 py-2 text-sm text-muted-foreground">
        <span className="flex items-center gap-1.5">
          <MapPin className="size-3.5 shrink-0" />
          {t("You hold no seat in any department.", "Sie haben in keiner Abteilung einen Platz.")}
        </span>
        <span className="mt-1 block text-xs">
          {t(
            "A seat is granted per department by an administrator and takes effect on your next request — no new sign-in.",
            "Ein Platz wird je Abteilung von einer Administratorin vergeben und gilt ab Ihrer nächsten Anfrage — ohne neue Anmeldung.",
          )}
        </span>
      </div>
    );
  }

  return (
    <div className="mt-4">
      <div className="micro-label">{t("Your seats", "Ihre Plätze")}</div>
      <ul className="mt-2 grid gap-1.5">
        {seats.map((seat) => (
          <li
            key={seat.departmentId}
            className="flex flex-wrap items-center gap-2 rounded border border-border px-2 py-1.5 text-xs"
          >
            <MapPin className="size-3.5 shrink-0 text-muted-foreground" />
            <span className="font-medium">
              {seat.departmentName || t("Unnamed department", "Unbenannte Abteilung")}
            </span>
            <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-micro">
              {seat.seatRole}
            </span>
            {/* What the seat role actually carries, read off the wire rather
                than restated here — the vocabulary is closed at four permissions
                in the backend and a second copy would drift from it. */}
            <span className="text-muted-foreground">
              {(seatRoles[seat.seatRole] ?? []).join(" · ")}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/* ------------------------------------------------------------- tenant roles */

/** The tenant's own roles: a LIST, then a DETAIL. Never a matrix.
 *
 * 52 permissions × N roles behind a horizontal scrollbar is a shape that stops
 * being readable at the third role and that nobody edits — you do not tick a
 * column. The list answers "which roles exist and who holds them", the detail
 * answers "what exactly does this one grant", and those are the two questions
 * anybody actually has.
 *
 * The whole panel is behind `role:view`, so it is fetched only when the caller
 * holds it. That is what keeps `GET /governance`'s ungated promise literally
 * true: the caller's own authority is explained to everybody above, and this
 * tenant's authority MAP — which roles exist, who holds them — is a separate,
 * gated endpoint. A seatless employee finds out why his screen is empty without
 * being able to read who in the company may release money.
 */
function TenantRoles({ data }: { data: Governance }) {
  const t = useT();
  // Read straight off the payload this panel was handed, NOT through `useCan()`.
  // `useCan` answers TRUE while the model is unresolved, which is right for the
  // sidebar and wrong here: it would fire `GET /roles` for a caller who is about
  // to be refused it, and put a red "could not be loaded" box on the one screen
  // whose entire job is to explain a refusal calmly to that exact person. The
  // parent renders nothing until `data` has landed, so here there is no
  // unresolved state to be optimistic about.
  const holds = (permission: string) => data.callerPermissions.includes(permission);
  const mayRead = holds("role:view");
  const mayManage = holds("role:manage");
  const mayAssign = holds("member:manage");

  const { data: roles, isLoading, error } = useRoles(mayRead);
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const tenantRoles = useMemo(() => (roles ?? []).filter((r) => !r.builtin), [roles]);
  const builtins = useMemo(() => (roles ?? []).filter((r) => r.builtin), [roles]);

  if (!mayRead) {
    return (
      <Panel className="p-6">
        <PanelHeading
          title={t("Roles of this company", "Rollen dieses Unternehmens")}
          hint={t("role:view", "role:view")}
        />
        <p className="mt-2 text-sm text-muted-foreground">
          {t(
            "Your role does not include role:view, so which roles this company has invented and who holds them is not shown to you. What you yourself may do is at the top of this page and needs no permission at all.",
            "Ihre Rolle enthält role:view nicht, deshalb wird Ihnen nicht angezeigt, welche Rollen dieses Unternehmen angelegt hat und wer sie hält. Was Sie selbst dürfen, steht oben auf dieser Seite und braucht keine Berechtigung.",
          )}
        </p>
      </Panel>
    );
  }

  return (
    <Panel className="p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <PanelHeading
            title={t("Roles of this company", "Rollen dieses Unternehmens")}
            hint={t("you define these", "von Ihnen definiert")}
          />
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
            {t(
              "A role is a name and a set of rights — nothing else. It applies company-wide: WHERE somebody works is decided by their seat in a department, not by their role. A change here takes effect on every holder’s next request, with the token they are already carrying.",
              "Eine Rolle ist ein Name und eine Menge von Rechten — mehr nicht. Sie gilt unternehmensweit: WO jemand arbeitet, entscheidet sein Platz in einer Abteilung, nicht seine Rolle. Eine Änderung hier gilt ab der nächsten Anfrage jedes Halters, mit dem Token, das er ohnehin schon trägt.",
            )}
          </p>
        </div>
        {mayManage && !creating && (
          <button
            type="button"
            onClick={() => {
              setSelected(null);
              setCreating(true);
            }}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:brightness-110"
          >
            <Plus className="size-4" /> {t("New role", "Neue Rolle")}
          </button>
        )}
      </div>

      {isLoading && (
        <SkeletonRows
          label={t("Loading roles…", "Rollen werden geladen…")}
          rowClassName="h-10"
          className="mt-4"
        />
      )}
      {error && (
        <p className="mt-4 text-sm text-destructive">
          {t("The role list could not be loaded.", "Die Rollenliste konnte nicht geladen werden.")}
        </p>
      )}

      {!isLoading && !error && tenantRoles.length === 0 && !creating && (
        <div className="mt-4 rounded-md border border-dashed border-border bg-muted/20 px-4 py-6 text-center">
          <p className="text-sm text-muted-foreground">
            {t(
              "This company has not defined any roles of its own. As long as everybody may do everything, you do not need any.",
              "Dieses Unternehmen hat noch keine eigenen Rollen definiert. Solange jeder alles darf, brauchen Sie keine.",
            )}
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            {t(
              "The day somebody should only sign off offers: fork a built-in role, untick everything but four boxes, name it, assign it.",
              "An dem Tag, an dem jemand nur noch Angebote freigeben soll: eine eingebaute Rolle als Vorlage nehmen, alles bis auf vier Häkchen abwählen, benennen, zuweisen.",
            )}
          </p>
          {mayManage && (
            <button
              type="button"
              onClick={() => setCreating(true)}
              className="mt-3 inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm transition hover:bg-muted"
            >
              <Plus className="size-3.5" /> {t("Create the first role", "Erste Rolle anlegen")}
            </button>
          )}
        </div>
      )}

      {tenantRoles.length > 0 && (
        <ul className="mt-4 grid gap-1.5">
          {tenantRoles.map((role) => (
            <li key={role.id ?? role.name}>
              <button
                type="button"
                onClick={() => {
                  setCreating(false);
                  setSelected((prev) => (prev === role.id ? null : role.id));
                }}
                aria-expanded={selected === role.id}
                className={`flex w-full items-center gap-3 rounded-md border px-3 py-2 text-left text-sm transition ${
                  selected === role.id
                    ? "border-primary/50 bg-primary/5"
                    : "border-border hover:bg-muted/40"
                }`}
              >
                <ChevronRight
                  className={`size-3.5 shrink-0 text-muted-foreground transition-transform ${
                    selected === role.id ? "rotate-90" : ""
                  }`}
                />
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-medium">{roleLabel(role.name, t)}</span>
                  {role.description && (
                    <span className="block truncate text-xs text-muted-foreground">
                      {role.description}
                    </span>
                  )}
                </span>
                <span className="shrink-0 text-xs text-muted-foreground">
                  {t(
                    `${role.permissions.length} rights`,
                    `${role.permissions.length} Recht${role.permissions.length === 1 ? "" : "e"}`,
                  )}
                </span>
                <span className="inline-flex shrink-0 items-center gap-1 rounded border border-border px-1.5 py-0.5 text-xs text-muted-foreground">
                  <Users className="size-3" />
                  {role.holderCount}
                </span>
              </button>
              {selected === role.id && role.id && (
                <RoleDetail
                  roleId={role.id}
                  mayManage={mayManage}
                  mayAssign={mayAssign}
                  // Every role with a row, not only the tenant's own: moving
                  // holders onto a built-in row is a legitimate landing place
                  // and is the honest alternative to letting them fall back to
                  // whatever their token says.
                  siblings={roles ?? []}
                  onGone={() => setSelected(null)}
                />
              )}
            </li>
          ))}
        </ul>
      )}

      {creating && (
        <RoleComposer
          builtins={builtins.length > 0 ? builtins : data.roles}
          onDone={(id) => {
            setCreating(false);
            setSelected(id);
          }}
          onCancel={() => setCreating(false)}
        />
      )}
    </Panel>
  );
}

/** The editor for one existing role: what it grants, and who it changes.
 *
 * The holders are loaded with it and shown above Save, because "how many people
 * does this change, and which of them" is the only question that matters before
 * an edit, and a screen of checkboxes with no names asks somebody to click Save
 * on a number he cannot see. */
function RoleDetail({
  roleId,
  mayManage,
  mayAssign,
  siblings,
  onGone,
}: {
  roleId: string;
  mayManage: boolean;
  mayAssign: boolean;
  siblings: RoleSummary[];
  onGone: () => void;
}) {
  const t = useT();
  const { data: role, isLoading } = useRole(roleId);
  const update = useUpdateRole();
  const remove = useDeleteRole();
  const assign = useAssignRole();
  // The BULK route for adding holders: one transaction, one audit event per
  // person, and the reason onboarding is no longer one round trip each.
  const bulk = useBulkAssignRole();
  const { data: catalogue = [] } = usePermissionCatalogue();
  const { data: assigneesPage } = useAssignees(mayAssign);
  const assignees = assigneesPage?.items ?? [];

  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [description, setDescription] = useState("");
  const [reassignTo, setReassignTo] = useState("");
  const [seeded, setSeeded] = useState<string | null>(null);

  // Seeded ONCE, when the role first arrives — not on every change of the query
  // object. Every mutation on this panel invalidates `["roles"]`, and `["roles",
  // id]` is under that prefix, so assigning a holder refetches this role: an
  // effect that re-seeded on each refetch would silently throw away the boxes
  // somebody had just ticked, at the exact moment they were setting the role up
  // for the person they were assigning it to. `useState` is the right place for
  // an unsaved edit, and an unsaved edit belongs to the person making it.
  useEffect(() => {
    if (!role || seeded === roleId) return;
    setPicked(new Set(role.permissions));
    setDescription(role.description);
    setSeeded(roleId);
  }, [role, roleId, seeded]);

  if (isLoading || !role) {
    return (
      <SkeletonRows
        label={t("Loading the role…", "Rolle wird geladen…")}
        className="mt-1 rounded-md border border-border px-3 py-3"
      />
    );
  }

  const dirty =
    description !== role.description ||
    picked.size !== role.permissions.length ||
    role.permissions.some((p) => !picked.has(p));

  return (
    <div className="mt-1 rounded-md border border-border bg-background/30 p-4">
      <label className="block micro-label">
        {t("What this role is for", "Wofür diese Rolle da ist")}
      </label>
      <input
        value={description}
        disabled={!mayManage}
        onChange={(e) => setDescription(e.target.value)}
        placeholder={t("One sentence", "Ein Satz")}
        className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50 disabled:opacity-60"
      />

      <PermissionPicker
        catalogue={catalogue}
        picked={picked}
        onToggle={(p) =>
          setPicked((prev) => {
            const next = new Set(prev);
            if (next.has(p)) next.delete(p);
            else next.add(p);
            return next;
          })
        }
        disabled={!mayManage}
      />

      <Holders
        holders={role.holders}
        total={role.holderCount}
        mayAssign={mayAssign}
        onRemove={(memberId) =>
          assign.mutate(
            { memberId, roleId: null },
            {
              onSuccess: () =>
                toast.success(
                  t(
                    "Assignment cleared — that person’s token decides again.",
                    "Zuweisung entfernt — für diese Person entscheidet wieder ihr Token.",
                  ),
                ),
              onError: (e) => toast.error(e.message),
            },
          )
        }
      />

      {mayAssign && (
        <AssignPicker
          assignees={assignees}
          roleId={roleId}
          roleName={role.name}
          holderIds={new Set(role.holders.map((h) => h.memberId))}
          pending={bulk.isPending}
          onAssign={(memberIds) =>
            bulk.mutate(
              { memberIds, roleId },
              {
                onSuccess: () =>
                  toast.success(
                    t(
                      `Assigned to ${memberIds.length} — effective on their next request.`,
                      `${memberIds.length} Person(en) zugewiesen — gilt ab deren nächster Anfrage.`,
                    ),
                  ),
                onError: (e) => toast.error(e.message),
              },
            )
          }
        />
      )}

      {mayManage && (
        <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-border pt-3">
          <button
            type="button"
            disabled={!dirty || update.isPending}
            onClick={() =>
              update.mutate(
                { roleId, description, permissions: [...picked] },
                {
                  onSuccess: (saved) =>
                    toast.success(
                      t(
                        `Saved — ${saved.holderCount} holder(s) are affected on their next request.`,
                        `Gespeichert — ${saved.holderCount} Halter sind ab ihrer nächsten Anfrage betroffen.`,
                      ),
                    ),
                  onError: (e) => toast.error(e.message),
                },
              )
            }
            className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground transition hover:brightness-110 disabled:opacity-50"
          >
            {update.isPending
              ? t("Saving…", "Wird gespeichert…")
              : t(
                  `Save for ${role.holderCount} holder(s)`,
                  `Für ${role.holderCount} Halter speichern`,
                )}
          </button>

          {/* A held role is not deleted and left to fall back: the holders would
              be restored to whatever their TOKEN says, which on every live
              tenant is `org_admin`. Where they go is asked for HERE rather than
              after a 409. */}
          {role.holderCount > 0 && (
            <select
              value={reassignTo}
              onChange={(e) => setReassignTo(e.target.value)}
              aria-label={t("Move holders to", "Halter übertragen an")}
              className="rounded-md border border-border bg-background/40 px-2 py-1.5 text-sm outline-none focus:border-primary/50"
            >
              <option value="">{t("Move holders to…", "Halter übertragen an…")}</option>
              {siblings
                .filter((s) => s.id && s.id !== roleId)
                .map((s) => (
                  <option key={s.id} value={s.id ?? ""}>
                    {s.name}
                  </option>
                ))}
            </select>
          )}
          <button
            type="button"
            disabled={remove.isPending || (role.holderCount > 0 && !reassignTo)}
            title={
              role.holderCount > 0 && !reassignTo
                ? t(
                    "Somebody holds this role. Choose where they go first.",
                    "Diese Rolle wird gehalten. Wählen Sie zuerst, wohin die Halter wechseln.",
                  )
                : undefined
            }
            onClick={() =>
              remove.mutate(
                { roleId, reassignTo: reassignTo || null },
                {
                  onSuccess: () => {
                    onGone();
                    toast.success(t("Role deleted.", "Rolle gelöscht."));
                  },
                  onError: (e) => toast.error(e.message),
                },
              )
            }
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm text-muted-foreground transition hover:text-status-error disabled:opacity-50"
          >
            <Trash2 className="size-3.5" /> {t("Delete", "Löschen")}
          </button>
          <span className="text-xs text-muted-foreground">
            {t(
              "The name stays taken after a delete — it is never handed to a different role.",
              "Der Name bleibt nach dem Löschen belegt — er wird nie an eine andere Rolle vergeben.",
            )}
          </span>
        </div>
      )}
    </div>
  );
}

/** Composing a new role. The name is asked for once and never again: renaming is
 *  refused, because the name is what the audit trail says and a rename would
 *  retroactively change what every earlier entry appears to be about. */
function RoleComposer({
  builtins,
  onDone,
  onCancel,
}: {
  builtins: Array<RoleSummary | GovernanceRole>;
  onDone: (id: string | null) => void;
  onCancel: () => void;
}) {
  const t = useT();
  const create = useCreateRole();
  const { data: catalogue = [] } = usePermissionCatalogue();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [basedOn, setBasedOn] = useState("");
  const [picked, setPicked] = useState<Set<string>>(new Set());

  const delegatable = useMemo(
    () => new Set(catalogue.filter((c) => c.delegatable).map((c) => c.permission)),
    [catalogue],
  );

  return (
    <div className="mt-4 rounded-md border border-primary/40 bg-primary/5 p-4">
      <PanelHeading
        title={t("New role", "Neue Rolle")}
        hint={t("name + rights", "Name + Rechte")}
      />
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <div>
          <label className="block micro-label">{t("Name", "Name")}</label>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={t("e.g. Sales sign-off", "z. B. Freigabe Vertrieb")}
            className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
          />
          <p className="mt-1 text-xs text-muted-foreground">
            {t(
              "Cannot be changed later, and cannot be the name of a built-in role.",
              "Später nicht änderbar und nicht der Name einer eingebauten Rolle.",
            )}
          </p>
        </div>
        <div>
          <label className="block micro-label">{t("Start from", "Vorlage")}</label>
          <select
            value={basedOn}
            onChange={(e) => {
              const chosen = e.target.value;
              setBasedOn(chosen);
              const preset = builtins.find((b) => b.name === chosen);
              // Only the rights a tenant role may actually hold are pre-ticked.
              // Pre-ticking a refused one would show a box the Save button then
              // refuses, and the point of the preset is to save clicks.
              setPicked(new Set((preset?.permissions ?? []).filter((p) => delegatable.has(p))));
            }}
            aria-label={t("Start from", "Vorlage")}
            className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
          >
            <option value="">{t("Empty", "Leer")}</option>
            {builtins.map((b) => (
              <option key={b.name} value={b.name}>
                {b.name}
              </option>
            ))}
          </select>
          <p className="mt-1 text-xs text-muted-foreground">
            {t(
              "A template only — it ticks boxes now and is not stored. There is no inheritance.",
              "Nur eine Vorlage — sie setzt jetzt Häkchen und wird nicht gespeichert. Es gibt keine Vererbung.",
            )}
          </p>
        </div>
      </div>
      <div className="mt-3">
        <label className="block micro-label">
          {t("What this role is for", "Wofür diese Rolle da ist")}
        </label>
        <input
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder={t("One sentence", "Ein Satz")}
          className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
        />
      </div>

      <PermissionPicker
        catalogue={catalogue}
        picked={picked}
        onToggle={(p) =>
          setPicked((prev) => {
            const next = new Set(prev);
            if (next.has(p)) next.delete(p);
            else next.add(p);
            return next;
          })
        }
      />

      <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-border pt-3">
        <button
          type="button"
          disabled={!name.trim() || create.isPending}
          onClick={() =>
            create.mutate(
              {
                name: name.trim(),
                description,
                permissions: [...picked],
                basedOn: basedOn || null,
              },
              {
                onSuccess: (created) => {
                  // The id, not a placeholder: `setSelected("")` would match no
                  // row and the freshly created role would land collapsed.
                  onDone(created.id);
                  toast.success(t("Role created.", "Rolle angelegt."));
                },
                onError: (e) => toast.error(e.message),
              },
            )
          }
          className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground transition hover:brightness-110 disabled:opacity-50"
        >
          {create.isPending ? t("Creating…", "Wird angelegt…") : t("Create role", "Rolle anlegen")}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md border border-border px-3 py-1.5 text-sm text-muted-foreground transition hover:text-foreground"
        >
          {t("Cancel", "Abbrechen")}
        </button>
        <span className="text-xs text-muted-foreground">
          {t(
            `${picked.size} of ${delegatable.size} offerable rights ticked`,
            `${picked.size} von ${delegatable.size} vergebbaren Rechten angehakt`,
          )}
        </span>
      </div>
    </div>
  );
}

/** The 52 checkboxes, grouped by resource, with the 31 refused ones SHOWN.
 *
 * Shown and disabled, with the backend's own reason inline. Hiding them would
 * produce the support ticket asking where the setting went, and the reason
 * string is the same string the API answers a 422 with — so the sentence on the
 * screen and the sentence in the refusal cannot drift apart.
 *
 * Grouped with `byResource`, which is exported from `governance-hooks` for
 * exactly this, so the panel above and this editor group identically. */
function PermissionPicker({
  catalogue,
  picked,
  onToggle,
  disabled = false,
}: {
  catalogue: PermissionInfo[];
  picked: Set<string>;
  onToggle: (permission: string) => void;
  disabled?: boolean;
}) {
  const t = useT();
  const { lang } = useLang();
  // REACHABLE by default, not SHOWN by default, and those are different
  // promises. A hidden control produces a support ticket asking where the
  // setting went — so the switch is one click away, on the panel, permanently
  // labelled with what it reveals and why.
  //
  // Shown by default was the wrong reading of that. The panel opens with the 52
  // rights across 23 resource cards, 31 of them greyed out and SIX whole cards
  // (agent, department, handoff, integration, secret, tool) containing nothing
  // tickable at all — a form whose first screenful is entirely things you cannot
  // do. The person composing a role wants the 21; the person reading a refusal
  // wants the 52, and he arrives from a 403 with a permission string in his hand
  // and one click to make. Defaulting to the second reader on the panel built for
  // the first is the whole of it.
  const [showRefused, setShowRefused] = useState(false);

  const info = useMemo(() => {
    const map = new Map<string, PermissionInfo>();
    for (const entry of catalogue) map.set(entry.permission, entry);
    return map;
  }, [catalogue]);
  const grouped = useMemo(() => byResource(catalogue.map((c) => c.permission)), [catalogue]);

  const label = (entry: PermissionInfo) =>
    lang === "de" ? entry.labelDe || entry.label : entry.label;
  const describe = (entry: PermissionInfo) =>
    lang === "de" ? entry.descriptionDe || entry.description : entry.description;

  if (catalogue.length === 0) {
    return (
      <p className="mt-4 text-sm text-muted-foreground">
        {t("Loading the rights…", "Rechte werden geladen…")}
      </p>
    );
  }

  return (
    <div className="mt-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="micro-label">{t("Rights", "Rechte")}</div>
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <input
            type="checkbox"
            checked={showRefused}
            onChange={(e) => setShowRefused(e.target.checked)}
            className="size-3.5 accent-[color:var(--primary)]"
          />
          {t(
            "Show the rights a role may not hold, and why",
            "Rechte anzeigen, die eine Rolle nicht halten darf — und warum",
          )}
        </label>
      </div>

      <div className="mt-2 grid gap-3 md:grid-cols-2">
        {[...grouped.entries()].map(([resource, permissions]) => {
          const entries = permissions
            .map((p) => info.get(p))
            .filter((e): e is PermissionInfo => e !== undefined)
            .filter((e) => e.delegatable || showRefused)
            // Offerable first: the boxes somebody can actually tick should not
            // be interleaved with greyed-out ones.
            .sort((a, b) => Number(b.delegatable) - Number(a.delegatable));
          if (entries.length === 0) return null;
          return (
            <div key={resource} className="rounded-md border border-border p-3">
              <div className="font-mono text-xs text-muted-foreground">{resource}</div>
              <ul className="mt-2 grid gap-1.5">
                {entries.map((entry) => (
                  <li key={entry.permission}>
                    <label
                      className={`flex items-start gap-2 text-sm ${
                        entry.delegatable ? "" : "opacity-60"
                      }`}
                    >
                      <input
                        type="checkbox"
                        className="mt-0.5 size-3.5 shrink-0 accent-[color:var(--primary)]"
                        checked={picked.has(entry.permission)}
                        disabled={disabled || !entry.delegatable}
                        onChange={() => onToggle(entry.permission)}
                      />
                      <span className="min-w-0">
                        <span className="flex flex-wrap items-center gap-1.5">
                          <span>{label(entry)}</span>
                          <span className="font-mono text-xs text-muted-foreground">
                            {entry.permission}
                          </span>
                          {!entry.delegatable && (
                            <Lock className="size-3 shrink-0 text-muted-foreground" />
                          )}
                        </span>
                        {describe(entry) && (
                          <span className="block text-xs text-muted-foreground">
                            {describe(entry)}
                          </span>
                        )}
                        {!entry.delegatable && entry.reason && (
                          <span className="mt-0.5 block text-xs text-status-warning">
                            {t("Not available: ", "Nicht vergebbar: ")}
                            {entry.reason}
                          </span>
                        )}
                      </span>
                    </label>
                  </li>
                ))}
              </ul>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** Search, tick, assign — for a tenant of three and for a tenant of five hundred.
 *
 * This replaced a `<select>` of everybody, and it replaced it three times over:
 *
 * * **It could not reach most of the company.** The query sent no `limit`, so the
 *   backend's default of 200 decided, ordered by `created_at` — the two hundred
 *   OLDEST people, with every later joiner unreachable and no notice that the
 *   list had been cut. Onboarding five hundred people "in the product" was
 *   literally impossible for three hundred of them.
 * * **It could not say what it was replacing.** An assignment overwrites whatever
 *   the person holds, and the option showed a name and nothing else, so the
 *   administrator's only record of the previous role was an audit event no screen
 *   renders. Every row now carries its current role.
 * * **It was one person per round trip.** `POST /members/roles:bulk` and
 *   `useBulkAssignRole` both existed and nothing called either; onboarding was
 *   five hundred single assignments through a dropdown holding two hundred of
 *   them. Ticking is multi-select and Assign is one call, one transaction, one
 *   audit event per person.
 *
 * The ceiling is still real and is SAID rather than hidden: past
 * `ASSIGNEE_LIMIT` the list is genuinely incomplete, and the sentence points at
 * `oc8 member import --csv`, which is the bridge the design names. A truncated
 * list that looks complete is that sentence with the honesty removed.
 */
function AssignPicker({
  assignees,
  roleId,
  roleName,
  holderIds,
  pending,
  onAssign,
}: {
  assignees: RoleAssignee[];
  roleId: string;
  roleName: string;
  holderIds: Set<string>;
  pending: boolean;
  onAssign: (memberIds: string[]) => void;
}) {
  const t = useT();
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<Set<string>>(new Set());

  // The holder list on a role DTO is a preview capped by the backend, so
  // filtering by it alone would offer somebody who already holds the role. Their
  // own `roleId` is the authoritative answer and is checked as well; assigning
  // twice is harmless and audited, but offering it is a screen that does not know
  // what it is looking at.
  const candidates = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return assignees.filter((a) => {
      if (holderIds.has(a.id) || a.roleId === roleId) return false;
      if (!needle) return true;
      return (
        a.displayName.toLowerCase().includes(needle) || a.subject.toLowerCase().includes(needle)
      );
    });
  }, [assignees, holderIds, query, roleId]);

  const truncated = assignees.length >= ASSIGNEE_LIMIT;
  const shown = candidates.slice(0, 200);

  return (
    <div className="mt-3 rounded-md border border-border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <label className="micro-label">
          {t(
            `Assign ${roleLabel(roleName, t)} to`,
            `${roleLabel(roleName, t)} zuweisen an`,
          )}
        </label>
        <span className="text-xs text-muted-foreground">
          {t(
            `${candidates.length} available · ${picked.size} selected`,
            `${candidates.length} verfügbar · ${picked.size} ausgewählt`,
          )}
        </span>
      </div>

      <input
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder={t("Search by name or sign-in", "Nach Name oder Anmeldung suchen")}
        aria-label={t("Search people", "Personen suchen")}
        className="mt-2 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
      />

      {candidates.length === 0 ? (
        <p className="mt-2 text-xs text-muted-foreground">
          {t(
            "Nobody left to assign. People appear here after they have signed in once.",
            "Niemand mehr zuzuweisen. Personen erscheinen hier, nachdem sie sich einmal angemeldet haben.",
          )}
        </p>
      ) : (
        <ul className="mt-2 max-h-56 space-y-0.5 overflow-y-auto pr-1">
          {shown.map((a) => (
            <li key={a.id}>
              <label className="flex cursor-pointer items-center gap-2 rounded px-1 py-1 text-sm hover:bg-muted/50">
                <input
                  type="checkbox"
                  className="size-3.5 shrink-0 accent-[color:var(--primary)]"
                  checked={picked.has(a.id)}
                  onChange={() =>
                    setPicked((prev) => {
                      const next = new Set(prev);
                      if (next.has(a.id)) next.delete(a.id);
                      else next.add(a.id);
                      return next;
                    })
                  }
                />
                <span className="min-w-0 truncate" title={a.subject}>
                  {a.displayName || a.subject}
                </span>
                {/* What the assignment REPLACES. Absent means the token decides,
                    which is not "no permissions" and is said as itself. */}
                <span className="ml-auto shrink-0 text-xs text-muted-foreground">
                  {a.roleName ? roleLabel(a.roleName, t) : t("from their sign-in", "aus ihrer Anmeldung")}
                </span>
              </label>
            </li>
          ))}
        </ul>
      )}

      {shown.length < candidates.length && (
        <p className="mt-1 text-xs text-muted-foreground">
          {t(
            `Showing ${shown.length} of ${candidates.length} — narrow the search.`,
            `${shown.length} von ${candidates.length} angezeigt — Suche eingrenzen.`,
          )}
        </p>
      )}
      {truncated && (
        <p className="mt-1 text-xs text-status-warning">
          {t(
            `This tenant has more than ${ASSIGNEE_LIMIT} people, so this list is incomplete. Use oc8 member import --csv for the rest.`,
            `Dieser Mandant hat mehr als ${ASSIGNEE_LIMIT} Personen — diese Liste ist unvollständig. Für den Rest: oc8 member import --csv.`,
          )}
        </p>
      )}

      <button
        type="button"
        disabled={picked.size === 0 || pending}
        onClick={() => {
          onAssign([...picked]);
          setPicked(new Set());
        }}
        className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm transition hover:bg-muted disabled:opacity-50"
      >
        <UserPlus className="size-3.5" />
        {picked.size > 1
          ? t(`Assign to ${picked.size} people`, `${picked.size} Personen zuweisen`)
          : t("Assign", "Zuweisen")}
      </button>
    </div>
  );
}

/** Who holds this role — a NAMED PREVIEW over an exact count.
 *
 * `holders` is capped by the backend and `holderCount` is not, because the two
 * answer different questions: "how many people does this change" has to be
 * right, and "which of them" is a list somebody reads. *Mitarbeiter* on the
 * five-hundred-person tenant is one role held by four hundred people, and this
 * DTO is the response model of four writes — assigning ONE person serialised all
 * four hundred names and drew four hundred chips.
 *
 * The gap is said out loud rather than left to look like the whole truth. */
function Holders({
  holders,
  total,
  mayAssign,
  onRemove,
}: {
  holders: Array<{ memberId: string; subject: string; displayName: string }>;
  total: number;
  mayAssign: boolean;
  onRemove: (memberId: string) => void;
}) {
  const t = useT();
  if (holders.length === 0) {
    return (
      <p className="mt-3 text-xs text-muted-foreground">
        {t("Nobody holds this role yet.", "Diese Rolle hält bisher niemand.")}
      </p>
    );
  }
  return (
    <div className="mt-3">
      <div className="micro-label">
        {total > holders.length
          ? t(
              `Held by ${total} — showing ${holders.length}`,
              `Gehalten von ${total} — ${holders.length} angezeigt`,
            )
          : t("Held by", "Gehalten von")}
      </div>
      <ul className="mt-1.5 flex flex-wrap gap-1.5">
        {holders.map((h) => (
          <li
            key={h.memberId}
            className="inline-flex items-center gap-1.5 rounded border border-border px-2 py-1 text-xs"
          >
            <span title={h.subject}>{h.displayName || h.subject}</span>
            {mayAssign && (
              <button
                type="button"
                onClick={() => onRemove(h.memberId)}
                aria-label={t(
                  `Clear the assignment for ${h.displayName || h.subject}`,
                  `Zuweisung für ${h.displayName || h.subject} entfernen`,
                )}
                title={t(
                  "Clear the assignment — their token decides again",
                  "Zuweisung entfernen — dann entscheidet wieder ihr Token",
                )}
                className="text-muted-foreground transition hover:text-status-error"
              >
                <Minus className="size-3" />
              </button>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

/* --------------------------------------------------------- the two read-only */

/** Permissions down, roles across. Grouped by resource so the shape of the
 * model is legible without reading 60 rows of `resource:action`.
 *
 * This survives as a matrix, and only this: five columns, fixed in code, that
 * nobody edits. The tenant's own roles above are a list-then-detail for exactly
 * the reason this one works — N columns do not. */
function BuiltInRoles({ data }: { data: Governance }) {
  const t = useT();
  const humans = data.roles.filter((r) => (r.kind ?? "human") === "human");
  const grouped = byResource(data.permissions);
  const held = new Map(humans.map((r) => [r.name, new Set(r.permissions)]));

  return (
    <Panel className="p-6">
      <PanelHeading
        title={t("Defined in code — not editable", "Im Code definiert — nicht änderbar")}
        hint={t("the ladder", "die Leiter")}
      />
      <p className="mt-1 text-sm text-muted-foreground">
        {t(
          "These five roles ship with the product, so no request’s authorization depends on a database row existing. They cannot be changed here — a role of your own, above, is how you say something they do not.",
          "Diese fünf Rollen liefert das Produkt mit, damit keine Autorisierung davon abhängt, dass eine Datenbankzeile existiert. Sie sind hier nicht änderbar — eine eigene Rolle, oben, ist der Weg, etwas zu sagen, was sie nicht sagen.",
        )}
      </p>

      {/* Wide content scrolls inside its own container; the page never does. */}
      <div className="mt-4 overflow-x-auto">
        <Table className="min-w-[36rem] border-collapse text-sm">
          <TableHeader>
            <TableRow className="border-b border-border">
              <TableHead className="px-2 py-2 micro-label">
                {t("Permission", "Berechtigung")}
              </TableHead>
              {humans.map((role) => (
                <TableHead
                  key={role.name}
                  className={`px-2 py-2 text-center font-mono text-xs font-medium ${
                    role.name === data.callerRole ? "text-foreground" : "text-muted-foreground"
                  }`}
                >
                  {role.name}
                  {role.name === data.callerRole && (
                    <span className="ml-1 micro-label">{t("you", "Sie")}</span>
                  )}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {[...grouped.entries()].map(([resource, permissions]) => (
              <ResourceRows
                key={resource}
                resource={resource}
                permissions={permissions}
                roles={humans.map((r) => r.name)}
                held={held}
              />
            ))}
          </TableBody>
        </Table>
      </div>
    </Panel>
  );
}

/** The agents' own roles, as their own table.
 *
 * Not a column in the matrix above, which is where `agent_default` used to be:
 * leftmost, alphabetically first, `tool:send` sitting as a row beside
 * `plugin:manage` as though a person could be given either. They are a different
 * vocabulary for a different population, and the backend now keeps them apart in
 * a column with a CHECK constraint — this is that separation, said on screen. */
function AgentRoles({ data }: { data: Governance }) {
  const t = useT();
  const agents = data.agentRoles ?? [];
  if (agents.length === 0) return null;

  return (
    <Panel className="p-6">
      <PanelHeading
        title={t("Agent roles", "Rollen für Agenten")}
        hint={t("not for people", "nicht für Menschen")}
      />
      <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
        {t(
          "These describe what an AGENT may do in your systems. They are never given to a person, and they can only ever subtract: what an agent actually does is this, intersected with its department’s frame and with its own narrowing.",
          "Diese beschreiben, was ein AGENT in Ihren Systemen tun darf. Sie werden nie an einen Menschen vergeben und können immer nur einschränken: Was ein Agent tatsächlich tut, ist diese Menge, geschnitten mit dem Rahmen seiner Abteilung und seiner eigenen Einschränkung.",
        )}
      </p>
      <ul className="mt-3 grid gap-1.5">
        {agents.map((role) => (
          <li
            key={role.name}
            className="flex flex-wrap items-center gap-2 rounded border border-border px-3 py-2 text-sm"
          >
            <Bot className="size-3.5 shrink-0 text-muted-foreground" />
            <span className="font-mono text-xs">{role.name}</span>
            <span className="text-xs text-muted-foreground">
              {role.permissions.join(" · ") || t("grants nothing", "gewährt nichts")}
            </span>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

function PanelHeading({ title, hint }: { title: string; hint: string }) {
  return (
    <div>
      <div className="micro-label">{hint}</div>
      <h2 className="font-serif text-xl">{title}</h2>
    </div>
  );
}

function ResourceRows({
  resource,
  permissions,
  roles,
  held,
}: {
  resource: string;
  permissions: string[];
  roles: string[];
  held: Map<string, Set<string>>;
}) {
  return (
    <>
      <TableRow className="border-b border-border/50 bg-muted/30">
        <TableCell colSpan={roles.length + 1} className="px-2 py-1.5 font-mono text-xs">
          {resource}
        </TableCell>
      </TableRow>
      {permissions.map((permission) => (
        <TableRow key={permission} className="border-b border-border/30">
          <TableCell className="px-2 py-1.5 pl-6 font-mono text-xs text-muted-foreground">
            {permission.split(":")[1]}
          </TableCell>
          {roles.map((role) => (
            <TableCell key={role} className="px-2 py-1.5 text-center">
              {held.get(role)?.has(permission) ? (
                <Check className="mx-auto size-3.5 text-status-running" aria-label="granted" />
              ) : (
                <Minus className="mx-auto size-3.5 text-muted-foreground/40" aria-label="—" />
              )}
            </TableCell>
          ))}
        </TableRow>
      ))}
    </>
  );
}
