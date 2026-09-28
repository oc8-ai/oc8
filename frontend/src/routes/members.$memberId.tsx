import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import {
  ArrowLeft,
  Check,
  Copy,
  KeyRound,
  Loader2,
  Mail,
  Save,
  ShieldCheck,
  Trash2,
  UserCog,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { Panel, roleLabel } from "@/components/app-shell";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useConfirm } from "@/hooks/use-confirm";
import { useMay } from "@/lib/governance-hooks";
import { useAuth, useAuthConfig, useDepartments } from "@/lib/hooks";
import { useT } from "@/lib/i18n";
import {
  useAssignees,
  useAssignRole,
  useCreateMember,
  useDeleteMember,
  useGrantSeat,
  useMintMemberPasswordReset,
  useRenameMemberSubject,
  useRevokeSeat,
  useRoles,
  useSetMemberPassword,
  type MemberSeat,
  type RoleAssignee,
} from "@/lib/roles-hooks";

export const Route = createFileRoute("/members/$memberId")({
  component: MemberDetail,
});

const SEAT_ROLES = ["dept_viewer", "dept_approver"] as const;
// Radix `Select.Item` refuses an empty-string value (it is reserved to mean
// "cleared, show the placeholder"), so "no role override" and "not
// provisioned for this tenant" each need a non-empty sentinel instead of the
// `null` / `""` the API itself uses.
const DEFAULT_ROLE_VALUE = "__sign_in_default__";

function MemberDetail() {
  const { memberId } = Route.useParams();
  const t = useT();
  const may = useMay();
  const mayManage = may("member:manage");

  const { data: membersPage, isLoading } = useAssignees(mayManage);
  const members = membersPage?.items ?? [];
  const member = members.find((m) => m.id === memberId);

  if (!mayManage) {
    return (
      <Panel className="p-6">
        <p className="text-sm text-muted-foreground">
          {t(
            "Your role does not include member:manage, so you cannot manage users.",
            "Ihre Rolle enthält member:manage nicht, daher können Sie Benutzer nicht verwalten.",
          )}
        </p>
      </Panel>
    );
  }

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 p-6 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" />
        {t("Loading…", "Wird geladen…")}
      </div>
    );
  }

  if (!member) {
    return (
      <Panel className="p-6">
        <Link
          to="/members"
          className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" /> {t("Back to users", "Zurück zu Benutzern")}
        </Link>
        <p className="mt-4 text-sm text-muted-foreground">
          {t("User not found.", "Benutzer nicht gefunden.")}
        </p>
      </Panel>
    );
  }

  return <MemberDetailForm member={member} />;
}

type Member = RoleAssignee;

export function MemberDetailForm({ member }: { member: Member }) {
  const t = useT();
  const { data: roles = [] } = useRoles(true);
  // Department picker for this member's seats, not a paginated list view.
  const { data: departmentsPage } = useDepartments({ pageSize: 200 });
  const departments = departmentsPage?.items ?? [];
  const { data: authConfig } = useAuthConfig();
  const updateMember = useCreateMember();
  const renameSubject = useRenameMemberSubject();
  const setPassword = useSetMemberPassword();
  const assignRole = useAssignRole();
  const grantSeat = useGrantSeat();
  const revokeSeat = useRevokeSeat();
  const mintReset = useMintMemberPasswordReset();
  const deleteMember = useDeleteMember();
  const navigate = useNavigate();
  const { data: me } = useAuth();
  const { confirm, ConfirmDialog } = useConfirm();
  const [pendingResetLink, setPendingResetLink] = useState<string | null>(null);
  const isSelf = me?.subject === member.subject;

  // Only Community's local-password login actually offers a password form to
  // USE a password set here.
  const localAuth = authConfig?.mode === "community";

  const [displayName, setDisplayName] = useState(member.displayName ?? "");
  useEffect(() => setDisplayName(member.displayName ?? ""), [member.displayName]);

  const [subject, setSubject] = useState(member.subject);
  useEffect(() => setSubject(member.subject), [member.subject]);

  const [newPassword, setNewPassword] = useState("");

  const orgAdminRole = roles.find((r) => r.name === "org_admin");
  const isAdmin = !!orgAdminRole?.id && member.roleId === orgAdminRole.id;

  const seats = member.seats ?? [];
  const seatedDepartmentIds = new Set(seats.map((s) => s.departmentId));
  const availableDepartments = useMemo(
    () => departments.filter((d) => !seatedDepartmentIds.has(d.id)),
    [departments, seatedDepartmentIds],
  );

  const [newSeatDepartmentId, setNewSeatDepartmentId] = useState("");
  const [newSeatRole, setNewSeatRole] = useState<string>(SEAT_ROLES[0]);
  const [newSeatAgentManage, setNewSeatAgentManage] = useState(false);

  const saveDisplayName = () => {
    updateMember.mutate(
      { subject: member.subject, displayName: displayName.trim() },
      {
        onSuccess: () => toast.success(t("Saved", "Gespeichert")),
        onError: (err) =>
          toast.error(
            err instanceof Error
              ? err.message
              : t("Could not save.", "Konnte nicht gespeichert werden."),
          ),
      },
    );
  };

  const saveSubject = () => {
    const trimmed = subject.trim();
    if (!trimmed || trimmed === member.subject) return;
    renameSubject.mutate(
      { memberId: member.id, subject: trimmed },
      {
        onSuccess: () =>
          toast.success(t("Sign-in identity updated", "Anmelde-Identität aktualisiert")),
        onError: (err) => {
          setSubject(member.subject);
          toast.error(
            err instanceof Error
              ? err.message
              : t("Could not save.", "Konnte nicht gespeichert werden."),
          );
        },
      },
    );
  };

  const submitPassword = () => {
    if (newPassword.length < 8) {
      toast.error(
        t(
          "Password must be at least 8 characters.",
          "Das Passwort muss mindestens 8 Zeichen haben.",
        ),
      );
      return;
    }
    setPassword.mutate(
      { memberId: member.id, password: newPassword },
      {
        onSuccess: () => {
          toast.success(t("Password set", "Passwort gesetzt"));
          setNewPassword("");
        },
        onError: (err) =>
          toast.error(
            err instanceof Error
              ? err.message
              : t("Could not set password.", "Passwort konnte nicht gesetzt werden."),
          ),
      },
    );
  };

  const setAdmin = (admin: boolean) => {
    if (admin && !orgAdminRole?.id) {
      toast.error(
        t(
          "The org_admin role is not set up for this tenant.",
          "Die Rolle org_admin ist für diesen Mandanten nicht angelegt.",
        ),
      );
      return;
    }
    assignRole.mutate(
      { memberId: member.id, roleId: admin ? orgAdminRole!.id : null },
      {
        onSuccess: () =>
          toast.success(
            admin
              ? t("Made an administrator", "Zum Administrator gemacht")
              : t("Made a standard user", "Zum Standard-User gemacht"),
          ),
        onError: (err) =>
          toast.error(
            err instanceof Error
              ? err.message
              : t("Could not assign role.", "Rolle konnte nicht zugewiesen werden."),
          ),
      },
    );
  };

  const changeAdvancedRole = (roleId: string) => {
    assignRole.mutate(
      { memberId: member.id, roleId: roleId === DEFAULT_ROLE_VALUE ? null : roleId },
      {
        onSuccess: () => toast.success(t("Role updated", "Rolle aktualisiert")),
        onError: (err) =>
          toast.error(
            err instanceof Error
              ? err.message
              : t("Could not assign role.", "Rolle konnte nicht zugewiesen werden."),
          ),
      },
    );
  };

  const setAllDepartments = (value: boolean) => {
    updateMember.mutate(
      { subject: member.subject, allDepartments: value },
      {
        onSuccess: () =>
          toast.success(
            value
              ? t("Granted company-wide access", "Unternehmensweiten Zugriff gewährt")
              : t("Revoked company-wide access", "Unternehmensweiten Zugriff entzogen"),
          ),
        onError: (err) =>
          toast.error(
            err instanceof Error
              ? err.message
              : t("Could not save.", "Konnte nicht gespeichert werden."),
          ),
      },
    );
  };

  const changeSeatRole = (departmentId: string, seatRole: string, agentManage: boolean) => {
    grantSeat.mutate(
      { memberId: member.id, departmentId, seatRole, agentManage },
      {
        onSuccess: () => toast.success(t("Seat updated", "Sitz aktualisiert")),
        onError: (err) =>
          toast.error(
            err instanceof Error
              ? err.message
              : t("Could not update seat.", "Sitz konnte nicht aktualisiert werden."),
          ),
      },
    );
  };

  const removeSeat = (departmentId: string) => {
    revokeSeat.mutate(
      { memberId: member.id, departmentId },
      {
        onSuccess: () => toast.success(t("Seat removed", "Sitz entfernt")),
        onError: (err) =>
          toast.error(
            err instanceof Error
              ? err.message
              : t("Could not remove seat.", "Sitz konnte nicht entfernt werden."),
          ),
      },
    );
  };

  const addSeat = () => {
    if (!newSeatDepartmentId) {
      toast.error(t("Choose a department.", "Wählen Sie ein Department."));
      return;
    }
    grantSeat.mutate(
      {
        memberId: member.id,
        departmentId: newSeatDepartmentId,
        seatRole: newSeatRole,
        agentManage: newSeatAgentManage,
      },
      {
        onSuccess: () => {
          toast.success(t("Seat added", "Sitz hinzugefügt"));
          setNewSeatDepartmentId("");
          setNewSeatRole(SEAT_ROLES[0]);
          setNewSeatAgentManage(false);
        },
        onError: (err) =>
          toast.error(
            err instanceof Error
              ? err.message
              : t("Could not add seat.", "Sitz konnte nicht hinzugefügt werden."),
          ),
      },
    );
  };

  const sendResetLink = () => {
    mintReset.mutate(member.id, {
      onSuccess: (result) => {
        if (result.resetSent) {
          toast.success(
            t("A reset email was sent to them", "Eine Reset-E-Mail wurde an sie verschickt"),
          );
        } else {
          setPendingResetLink(result.resetLink);
        }
      },
      onError: (err) =>
        toast.error(
          err instanceof Error
            ? err.message
            : t("Could not create a reset link.", "Reset-Link konnte nicht erstellt werden."),
        ),
    });
  };

  const removeMember = async () => {
    const ok = await confirm({
      title: t("Delete this user?", "Diesen Benutzer löschen?"),
      description: t(
        "They will disappear from the users list and will not be able to sign in. Unused invite and reset links are spent so they cannot come back through an old mail.",
        "Sie verschwinden aus der Benutzerliste und können sich nicht mehr anmelden. Ungenutzte Einladungs- und Reset-Links werden ungültig, damit sie nicht über eine alte Mail zurückkommen.",
      ),
      confirmLabel: t("Delete", "Löschen"),
      cancelLabel: t("Cancel", "Abbrechen"),
    });
    if (!ok) return;
    deleteMember.mutate(member.id, {
      onSuccess: () => {
        toast.success(t("User deleted", "Benutzer gelöscht"));
        navigate({ to: "/members" });
      },
      onError: (err) =>
        toast.error(
          err instanceof Error
            ? err.message
            : t("Could not delete this user.", "Benutzer konnte nicht gelöscht werden."),
        ),
    });
  };

  return (
    <div className="grid gap-4 p-6">
      <Link
        to="/members"
        className="inline-flex w-fit items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="h-4 w-4" /> {t("Back to users", "Zurück zu Benutzern")}
      </Link>

      <Panel className="p-6">
        <div className="flex items-start gap-3">
          <div className="grid h-9 w-9 place-items-center rounded-md bg-primary/15 text-primary">
            <UserCog className="h-4 w-4" />
          </div>
          <div>
            <h3 className="font-serif text-lg">{member.displayName || member.subject}</h3>
            {isAdmin && (
              <span className="mt-1 inline-flex items-center gap-1 rounded-full bg-primary/15 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-primary">
                <ShieldCheck className="size-3" />
                {t("Administrator", "Administrator")}
              </span>
            )}
            {member.firstSeenAt && (
              <p className="mt-1 text-xs text-muted-foreground">
                {t("First seen", "Zuerst gesehen")}: {new Date(member.firstSeenAt).toLocaleString()}
              </p>
            )}
          </div>
        </div>

        <div className="mt-6 grid gap-4 text-sm sm:max-w-md">
          <label className="grid gap-1.5">
            <span>{t("Display name", "Anzeigename")}</span>
            <div className="flex gap-2">
              <input
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                className="flex-1 rounded-md border border-input bg-background px-3 py-2"
              />
              <button
                type="button"
                onClick={saveDisplayName}
                disabled={updateMember.isPending || displayName === (member.displayName ?? "")}
                className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-xs font-medium text-primary-foreground disabled:opacity-50"
              >
                <Save className="size-3.5" />
                {t("Save", "Speichern")}
              </button>
            </div>
          </label>

          <label className="grid gap-1.5">
            <span>{t("Sign-in email", "Anmelde-E-Mail")}</span>
            {localAuth ? (
              <div className="flex gap-2">
                <input
                  value={subject}
                  onChange={(e) => setSubject(e.target.value)}
                  className="flex-1 rounded-md border border-input bg-background px-3 py-2"
                />
                <button
                  type="button"
                  onClick={saveSubject}
                  disabled={renameSubject.isPending || subject.trim() === member.subject}
                  className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-xs font-medium text-primary-foreground disabled:opacity-50"
                >
                  <Save className="size-3.5" />
                  {t("Save", "Speichern")}
                </button>
              </div>
            ) : (
              <input
                value={member.subject}
                readOnly
                disabled
                className="rounded-md border border-input bg-muted/30 px-3 py-2 text-muted-foreground"
              />
            )}
            {!localAuth && (
              <span className="text-xs text-muted-foreground">
                {t(
                  "Managed by your identity provider; not editable here.",
                  "Wird von Ihrem Identity-Provider verwaltet; hier nicht bearbeitbar.",
                )}
              </span>
            )}
          </label>
        </div>
      </Panel>

      {localAuth && (
        <Panel className="p-6">
          <div className="flex items-center gap-2">
            <KeyRound className="size-4 text-muted-foreground" />
            <h3 className="font-serif text-lg">{t("Password", "Passwort")}</h3>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            {t(
              "Set a new password so this person can sign in. There is no way to view their current password.",
              "Vergeben Sie ein neues Passwort, damit sich diese Person anmelden kann. Das aktuelle Passwort kann nicht eingesehen werden.",
            )}
          </p>
          <div className="mt-4 flex max-w-md gap-2 text-sm">
            <input
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              placeholder={t(
                "New password (min. 8 characters)",
                "Neues Passwort (mind. 8 Zeichen)",
              )}
              className="flex-1 rounded-md border border-input bg-background px-3 py-2"
            />
            <button
              type="button"
              onClick={submitPassword}
              disabled={setPassword.isPending || newPassword.length === 0}
              className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-xs font-medium text-primary-foreground disabled:opacity-50"
            >
              {setPassword.isPending
                ? t("Setting…", "Wird gesetzt…")
                : t("Set password", "Passwort setzen")}
            </button>
          </div>
          <div className="mt-6 border-t border-border pt-4">
            <p className="text-sm text-muted-foreground">
              {t(
                "Or send them a link to set their own password. The mail is tried first; if no mail server is configured, you get a copyable link instead.",
                "Oder senden Sie ihnen einen Link, damit sie selbst ein Passwort setzen. Zuerst wird die E-Mail versucht; ist kein Mailserver konfiguriert, erhalten Sie stattdessen einen kopierbaren Link.",
              )}
            </p>
            <button
              type="button"
              onClick={sendResetLink}
              disabled={mintReset.isPending}
              className="mt-3 inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-2 text-xs font-medium transition hover:bg-accent disabled:opacity-50"
            >
              <Mail className="size-3.5" />
              {mintReset.isPending
                ? t("Sending…", "Wird gesendet…")
                : t("Send reset link", "Reset-Link senden")}
            </button>
          </div>
        </Panel>
      )}

      <Panel className="p-6">
        <h3 className="font-serif text-lg">{t("Access", "Zugriff")}</h3>

        <div className="mt-4 flex items-center justify-between gap-4 rounded-md border border-border p-3">
          <div>
            <p className="text-sm font-medium">{t("Administrator", "Administrator")}</p>
            <p className="text-xs text-muted-foreground">
              {t(
                "Full access to every screen and department. Off means a standard user, limited to their department seats.",
                "Voller Zugriff auf alle Bereiche und Departments. Aus bedeutet Standard-User, beschränkt auf seine Department-Sitze.",
              )}
            </p>
          </div>
          <Switch checked={isAdmin} onCheckedChange={setAdmin} disabled={assignRole.isPending} />
        </div>

        <div className="mt-4 flex items-center justify-between gap-4 rounded-md border border-border p-3">
          <div>
            <p className="text-sm font-medium">{t("All departments", "Alle Departments")}</p>
            <p className="text-xs text-muted-foreground">
              {t(
                "Sees and decides in every department, including ones created later.",
                "Sieht und entscheidet in jedem Department, auch in später erstellten.",
              )}
            </p>
          </div>
          <Switch
            checked={member.allDepartments ?? false}
            onCheckedChange={setAllDepartments}
            disabled={updateMember.isPending}
          />
        </div>

        <details className="mt-4">
          <summary className="cursor-pointer text-sm text-muted-foreground hover:text-foreground">
            {t("Advanced: exact role override", "Erweitert: genaue Rolle festlegen")}
          </summary>
          <div className="mt-2 grid gap-1.5 text-sm sm:max-w-md">
            <Select value={member.roleId ?? DEFAULT_ROLE_VALUE} onValueChange={changeAdvancedRole}>
              <SelectTrigger>
                <SelectValue placeholder={t("Use sign-in default", "Anmeldungs-Standard nutzen")} />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={DEFAULT_ROLE_VALUE}>
                  {t("Use sign-in default", "Anmeldungs-Standard nutzen")}
                </SelectItem>
                {roles.map((role) => (
                  <SelectItem
                    key={role.id ?? role.name}
                    value={role.id ?? `__unset_${role.name}__`}
                    disabled={!role.id}
                  >
                    {roleLabel(role.name, t)}
                    {!role.id ? ` (${t("not set up", "nicht angelegt")})` : ""}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </details>
      </Panel>

      <Panel className="p-6">
        <h3 className="font-serif text-lg">{t("Department seats", "Department-Sitze")}</h3>
        <p className="mt-1 text-sm text-muted-foreground">
          {t(
            "Where this person stands, and at which level.",
            "Wo diese Person steht, und auf welcher Ebene.",
          )}
        </p>

        {seats.length === 0 ? (
          <p className="mt-4 text-sm text-muted-foreground">
            {t("No department seats yet.", "Noch keine Department-Sitze.")}
          </p>
        ) : (
          <div className="mt-4 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border">
                  <th className="py-2 text-left font-medium text-muted-foreground">
                    {t("Department", "Department")}
                  </th>
                  <th className="py-2 text-left font-medium text-muted-foreground">
                    {t("Seat role", "Sitz-Rolle")}
                  </th>
                  <th className="py-2 text-left font-medium text-muted-foreground">
                    {t("Manages agents", "Verwaltet Agenten")}
                  </th>
                  <th className="py-2 text-right font-medium text-muted-foreground" />
                </tr>
              </thead>
              <tbody>
                {seats.map((seat) => (
                  <SeatRow
                    key={seat.departmentId}
                    seat={seat}
                    onChangeRole={(role) =>
                      changeSeatRole(seat.departmentId, role, seat.agentManage)
                    }
                    onToggleAgentManage={(value) =>
                      changeSeatRole(seat.departmentId, seat.seatRole, value)
                    }
                    onRemove={() => removeSeat(seat.departmentId)}
                    pending={grantSeat.isPending || revokeSeat.isPending}
                    t={t}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}

        {availableDepartments.length > 0 && (
          <div className="mt-6 grid gap-3 rounded-md border border-dashed border-border p-4 sm:grid-cols-[1fr_1fr_auto_auto] sm:items-end">
            <div className="grid gap-1.5 text-sm">
              <span>{t("Department", "Department")}</span>
              <Select value={newSeatDepartmentId} onValueChange={setNewSeatDepartmentId}>
                <SelectTrigger>
                  <SelectValue placeholder={t("Select…", "Auswählen…")} />
                </SelectTrigger>
                <SelectContent>
                  {availableDepartments.map((d) => (
                    <SelectItem key={d.id} value={d.id}>
                      {d.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-1.5 text-sm">
              <span>{t("Seat role", "Sitz-Rolle")}</span>
              <Select value={newSeatRole} onValueChange={setNewSeatRole}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {SEAT_ROLES.map((role) => (
                    <SelectItem key={role} value={role}>
                      {role}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <label className="flex items-center gap-2 text-sm">
              <Switch checked={newSeatAgentManage} onCheckedChange={setNewSeatAgentManage} />
              {t("Manages agents", "Verwaltet Agenten")}
            </label>
            <button
              type="button"
              onClick={addSeat}
              disabled={grantSeat.isPending}
              className="rounded-md bg-primary px-3 py-2 text-xs font-medium text-primary-foreground disabled:opacity-50"
            >
              {t("Add seat", "Sitz hinzufügen")}
            </button>
          </div>
        )}
      </Panel>

      {!isSelf && (
        <Panel className="p-6">
          <h3 className="font-serif text-lg">{t("Remove user", "Benutzer entfernen")}</h3>
          <p className="mt-1 text-sm text-muted-foreground">
            {t(
              "Soft-delete this person. They leave the users list and cannot sign in.",
              "Diese Person weich löschen. Sie verschwindet aus der Benutzerliste und kann sich nicht anmelden.",
            )}
          </p>
          <button
            type="button"
            onClick={removeMember}
            disabled={deleteMember.isPending}
            className="mt-4 inline-flex items-center gap-1.5 rounded-md border border-destructive/40 px-3 py-2 text-xs font-medium text-destructive transition hover:bg-destructive/10 disabled:opacity-50"
          >
            <Trash2 className="size-3.5" />
            {deleteMember.isPending
              ? t("Deleting…", "Wird gelöscht…")
              : t("Delete user", "Benutzer löschen")}
          </button>
        </Panel>
      )}

      <ResetLinkDialog
        link={pendingResetLink}
        onOpenChange={(open) => {
          if (!open) setPendingResetLink(null);
        }}
      />
      {ConfirmDialog}
    </div>
  );
}

/** Shown when `POST /members/{id}/password-reset` minted a link that could
 *  NOT be emailed -- the same fallback as the create-user invite dialog.
 *  When the mail WAS sent, the caller toasts instead and this never opens. */
function ResetLinkDialog({
  link,
  onOpenChange,
}: {
  link: string | null;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useT();
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    if (!link) return;
    await navigator.clipboard.writeText(link);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <Dialog open={link !== null} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("Share this reset link", "Diesen Reset-Link teilen")}</DialogTitle>
          <DialogDescription>
            {t(
              "No mail server is configured for this instance, so the reset could not be emailed. Copy this link and send it to them yourself — it lets them set a new password.",
              "Für diese Instanz ist kein Mailserver konfiguriert, daher konnte der Reset nicht per E-Mail versendet werden. Kopieren Sie diesen Link und senden Sie ihn selbst — damit kann ein neues Passwort gesetzt werden.",
            )}
          </DialogDescription>
        </DialogHeader>
        <div className="flex items-center gap-2">
          <input
            readOnly
            value={link ?? ""}
            onFocus={(e) => e.currentTarget.select()}
            className="flex-1 rounded-md border border-input bg-background px-3 py-2 text-xs"
          />
          <button
            type="button"
            onClick={copy}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-border px-3 py-2 text-xs font-medium transition hover:bg-accent"
          >
            {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
            {copied ? t("Copied", "Kopiert") : t("Copy", "Kopieren")}
          </button>
        </div>
        <div className="flex justify-end">
          <button
            onClick={() => onOpenChange(false)}
            className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground"
          >
            {t("Done", "Fertig")}
          </button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function SeatRow({
  seat,
  onChangeRole,
  onToggleAgentManage,
  onRemove,
  pending,
  t,
}: {
  seat: MemberSeat;
  onChangeRole: (role: string) => void;
  onToggleAgentManage: (value: boolean) => void;
  onRemove: () => void;
  pending: boolean;
  t: (en: string, de: string) => string;
}) {
  return (
    <tr className="border-b border-border/50">
      <td className="py-2">{seat.departmentName || seat.departmentId}</td>
      <td className="py-2">
        <Select value={seat.seatRole} onValueChange={onChangeRole}>
          <SelectTrigger className="h-8 w-40 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {SEAT_ROLES.map((role) => (
              <SelectItem key={role} value={role}>
                {role}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </td>
      <td className="py-2">
        <Switch
          checked={seat.agentManage}
          onCheckedChange={onToggleAgentManage}
          disabled={pending}
        />
      </td>
      <td className="py-2 text-right">
        <button
          type="button"
          onClick={onRemove}
          disabled={pending}
          title={t("Remove seat", "Sitz entfernen")}
          className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-destructive transition hover:bg-destructive/10 disabled:opacity-50"
        >
          <Trash2 className="size-3" />
        </button>
      </td>
    </tr>
  );
}
