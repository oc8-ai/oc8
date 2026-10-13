import { createFileRoute, Link } from "@tanstack/react-router";
import { Check, ChevronRight, Copy, Loader2, Plus, Users } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { Panel, roleLabel } from "@/components/app-shell";
import { ListToolbar, type ListQueryState } from "@/components/list-toolbar";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useAssignees, useCreateMember, type RoleAssignee } from "@/lib/roles-hooks";
import { ApiError } from "@/lib/api";
import { useAuthConfig } from "@/lib/hooks";
import { useMay } from "@/lib/governance-hooks";
import { useT } from "@/lib/i18n";

export const Route = createFileRoute("/members/")({
  component: MembersPage,
});

export function MembersPage() {
  const t = useT();
  const may = useMay();
  const mayManage = may("member:manage");
  const [createOpen, setCreateOpen] = useState(false);
  // Set only when `POST /members` minted an invite link that could NOT be
  // emailed (no mail server configured, or the send failed) -- the fallback
  // an administrator can still copy and hand to the new person by hand.
  // `inviteSent` links show a toast instead (see `CreateMemberDialog`) since
  // there is nothing left for the administrator to do with them.
  const [pendingInviteLink, setPendingInviteLink] = useState<string | null>(null);

  // Gate the whole page on member:manage so only admins can see it. This
  // check runs AFTER every hook above so hook order stays identical across
  // renders (Rules of Hooks) -- an early return before a hook call once
  // crashed the whole route with React error #310 the moment governance
  // resolved from "loading" to "denied".
  if (!mayManage) {
    return (
      <Panel className="p-6">
        <div className="flex items-start gap-3">
          <div className="grid h-9 w-9 place-items-center rounded-md bg-primary/15 text-primary">
            <Users className="h-4 w-4" />
          </div>
          <div>
            <h3 className="font-serif text-lg">{t("Users", "Benutzer")}</h3>
            <p className="mt-2 text-sm text-muted-foreground">
              {t(
                "Your role does not include member:manage, so you cannot manage users. Ask your administrator for access.",
                "Ihre Rolle enthält member:manage nicht, daher können Sie Benutzer nicht verwalten. Bitten Sie Ihren Administrator um Zugriff.",
              )}
            </p>
          </div>
        </div>
      </Panel>
    );
  }

  return (
    <Panel className="p-6">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-3">
          <div className="grid h-9 w-9 place-items-center rounded-md bg-primary/15 text-primary">
            <Users className="h-4 w-4" />
          </div>
          <div>
            <h3 className="font-serif text-lg">{t("Users", "Benutzer")}</h3>
            <p className="mt-1 text-sm text-muted-foreground">
              {t(
                "Manage user roles for your organization.",
                "Verwalten Sie Benutzerrollen für Ihre Organisation.",
              )}
            </p>
          </div>
        </div>
        <button
          type="button"
          onClick={() => setCreateOpen(true)}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition hover:opacity-90"
        >
          <Plus className="size-3.5" />
          {t("New user", "Neuer Benutzer")}
        </button>
      </div>

      <MembersTable />

      <CreateMemberDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onInviteLinkNotSent={setPendingInviteLink}
      />
      <InviteLinkDialog
        link={pendingInviteLink}
        onOpenChange={(open) => {
          if (!open) setPendingInviteLink(null);
        }}
      />
    </Panel>
  );
}

/** Shown once, right after `CreateMemberDialog` closes, ONLY when an invite
 *  link was minted but could not be emailed (no mail server configured, or
 *  the send failed) -- the fallback path the design calls for: a link an
 *  administrator can copy and hand to the new person by hand. When the link
 *  WAS emailed, `CreateMemberDialog` shows a plain success toast instead and
 *  this dialog never opens -- there is nothing left here to act on. */
function InviteLinkDialog({
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
          <DialogTitle>{t("Share this invite link", "Diesen Einladungslink teilen")}</DialogTitle>
          <DialogDescription>
            {t(
              "No mail server is configured for this instance, so the invite could not be emailed. Copy this link and send it to the new user yourself — it lets them set their own password.",
              "Für diese Instanz ist kein Mailserver konfiguriert, daher konnte die Einladung nicht per E-Mail versendet werden. Kopieren Sie diesen Link und senden Sie ihn selbst an den neuen Benutzer — damit kann er sein eigenes Passwort festlegen.",
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

/** Roles an invitation can carry, least to most; the deployment refuses one
 *  above the inviter's own. */
const INVITE_ROLES = ["member", "auditor", "operator", "dept_manager", "org_admin"];

interface InviteRefusal {
  message: string;
  /** Where the deployment suggests going next; only same-app paths are kept. */
  action: { label: string; url: string } | null;
}

/** A structured refusal from the deployment's inviter, else null. */
function inviteRefusal(err: unknown): InviteRefusal | null {
  if (!(err instanceof ApiError)) return null;
  let detail: { code?: string; message?: string; actionLabel?: string; actionUrl?: string };
  try {
    detail = JSON.parse(err.message);
  } catch {
    return null;
  }
  if (!detail || typeof detail.code !== "string" || typeof detail.message !== "string") return null;
  const url = detail.actionUrl;
  const sameApp = typeof url === "string" && url.startsWith("/") && !url.startsWith("//") && !url.startsWith("/\\");
  return {
    message: detail.message,
    action: sameApp && detail.actionLabel ? { label: detail.actionLabel, url } : null,
  };
}

function CreateMemberDialog({
  open,
  onOpenChange,
  onInviteLinkNotSent,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called with the invite link when one was minted but NOT emailed, so the
   *  parent can offer it in a copyable follow-up dialog. Never called for a
   *  member created with a password, in dev mode, or when the invite WAS
   *  emailed successfully. */
  onInviteLinkNotSent: (link: string) => void;
}) {
  const t = useT();
  const createMember = useCreateMember();
  const { data: authConfig } = useAuthConfig();
  // The deployment invites people itself: they get a role, no password,
  // and it may refuse with its own message.
  const externalInvites = authConfig?.memberInvites === "external";
  const localAuth = authConfig?.mode === "community" && !externalInvites;
  const [subject, setSubject] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("member");
  const [refusal, setRefusal] = useState<InviteRefusal | null>(null);

  const reset = () => {
    setSubject("");
    setDisplayName("");
    setPassword("");
    setRole("member");
    setRefusal(null);
  };

  const submit = () => {
    const trimmed = subject.trim();
    if (!trimmed) {
      toast.error(t("Enter a sign-in email or username.", "Geben Sie eine Anmelde-E-Mail oder einen Benutzernamen ein."));
      return;
    }
    if (localAuth && password.length > 0 && password.length < 8) {
      toast.error(t("Password must be at least 8 characters.", "Das Passwort muss mindestens 8 Zeichen haben."));
      return;
    }
    createMember.mutate(
      {
        subject: trimmed,
        displayName: displayName.trim(),
        ...(localAuth && password.length > 0 ? { password } : {}),
        ...(externalInvites ? { role } : {}),
      },
      {
        onSuccess: (created) => {
          if (created.inviteSent) {
            toast.success(
              t(
                "User created — an invite was emailed to them",
                "Benutzer angelegt — eine Einladung wurde per E-Mail verschickt",
              ),
            );
          } else {
            toast.success(t("User created", "Benutzer angelegt"));
            if (created.inviteLink) onInviteLinkNotSent(created.inviteLink);
          }
          reset();
          onOpenChange(false);
        },
        onError: (err) => {
          const refused = externalInvites ? inviteRefusal(err) : null;
          if (refused) {
            setRefusal(refused);
            return;
          }
          toast.error(
            err instanceof Error
              ? err.message
              : t("Could not create user. Try again.", "Benutzer konnte nicht angelegt werden. Versuchen Sie es erneut."),
          );
        },
      },
    );
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("New user", "Neuer Benutzer")}</DialogTitle>
          <DialogDescription>
            {t(
              "Enrol somebody by their sign-in identity. They can also be added automatically the first time they sign in — this just lets you set them up (and assign a role) ahead of time.",
              "Legen Sie jemanden über seine Anmelde-Identität an. Personen werden auch automatisch beim ersten Anmelden hinzugefügt — so können Sie vorab einrichten (und eine Rolle zuweisen).",
            )}
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-3 text-sm">
          <label className="grid gap-1.5">
            <span>{t("Sign-in email", "Anmelde-E-Mail")}</span>
            <input
              value={subject}
              onChange={(e) => setSubject(e.target.value)}
              placeholder="jane.doe@example.com"
              className="rounded-md border border-input bg-background px-3 py-2"
              autoFocus
            />
          </label>
          <label className="grid gap-1.5">
            <span>{t("Display name (optional)", "Anzeigename (optional)")}</span>
            <input
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="Jane Doe"
              className="rounded-md border border-input bg-background px-3 py-2"
            />
          </label>
          {externalInvites && (
            <label className="grid gap-1.5">
              <span>{t("Role", "Rolle")}</span>
              <select
                aria-label={t("Role", "Rolle")}
                value={role}
                onChange={(e) => setRole(e.target.value)}
                className="rounded-md border border-input bg-background px-3 py-2"
              >
                {INVITE_ROLES.map((name) => (
                  <option key={name} value={name}>
                    {roleLabel(name, t)}
                  </option>
                ))}
              </select>
            </label>
          )}
          {refusal && (
            <p role="alert" className="text-xs text-destructive">
              {refusal.message}
              {refusal.action && (
                <>
                  {" "}
                  <a href={refusal.action.url} className="underline">
                    {refusal.action.label}
                  </a>
                </>
              )}
            </p>
          )}
          {localAuth && (
            <label className="grid gap-1.5">
              <span>{t("Password (optional)", "Passwort (optional)")}</span>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={t("Leave empty to set up later", "Leer lassen, um später einzurichten")}
                className="rounded-md border border-input bg-background px-3 py-2"
              />
              <span className="text-xs text-muted-foreground">
                {t(
                  "Lets them sign in immediately. Min. 8 characters.",
                  "Ermöglicht sofortige Anmeldung. Mind. 8 Zeichen.",
                )}
              </span>
            </label>
          )}
        </div>
        <div className="flex justify-end gap-2">
          <button
            onClick={() => onOpenChange(false)}
            className="rounded-md border border-border px-3 py-1.5 text-xs"
          >
            {t("Cancel", "Abbrechen")}
          </button>
          <button
            onClick={submit}
            disabled={createMember.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-50"
          >
            {createMember.isPending
              ? t("Creating…", "Wird angelegt…")
              : externalInvites
                ? t("Invite", "Einladen")
                : t("Create user", "Benutzer anlegen")}
          </button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function MembersTable() {
  const t = useT();

  const [queryState, setQueryState] = useState<ListQueryState>({
    search: "",
    filters: {},
    groupBy: null,
    includeArchived: false,
    page: 1,
    pageSize: 20,
  });
  // Member has neither `SoftDeleteMixin` nor an archive concept (Design System
  // Consistency plan's Global Constraints excludes it explicitly), so only
  // `search`/`page`/`pageSize` are threaded through -- `queryState.filters`/
  // `groupBy`/`includeArchived` stay at their defaults and no matching config
  // is passed to <ListToolbar> below.
  const {
    data: assigneesPage,
    isLoading,
    error,
  } = useAssignees(true, {
    search: queryState.search,
    page: queryState.page,
    pageSize: queryState.pageSize,
  });
  const assignees = assigneesPage?.items ?? [];
  const totalCount = assigneesPage?.totalCount ?? 0;

  return (
    <div className="mt-6">
      <ListToolbar
        config={{ searchPlaceholder: t("Search members…", "Mitglieder suchen…") }}
        state={queryState}
        onStateChange={setQueryState}
        totalCount={totalCount}
      />

      {isLoading ? (
        <div className="mt-6 flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          {t("Loading users…", "Benutzer werden geladen…")}
        </div>
      ) : error ? (
        <p className="mt-6 text-sm text-destructive">
          {t("The user list could not be loaded.", "Die Benutzerliste konnte nicht geladen werden.")}
        </p>
      ) : assignees.length === 0 ? (
        <div className="mt-6 rounded-md border border-dashed border-border bg-muted/20 px-4 py-6 text-center">
          <p className="text-sm text-muted-foreground">
            {t(
              "No users found. Users appear here after they sign in.",
              "Keine Benutzer gefunden. Benutzer erscheinen hier, nachdem sie sich angemeldet haben.",
            )}
          </p>
        </div>
      ) : (
        <>
          <div className="mt-6 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border">
                  <th className="py-3 text-left font-medium text-muted-foreground">
                    {t("Name", "Name")}
                  </th>
                  <th className="py-3 text-left font-medium text-muted-foreground">
                    {t("Sign-in", "Anmeldung")}
                  </th>
                  <th className="py-3 text-left font-medium text-muted-foreground">
                    {t("Role", "Rolle")}
                  </th>
                  <th className="py-3 text-right font-medium text-muted-foreground">
                    {t("Action", "Aktion")}
                  </th>
                </tr>
              </thead>
              <tbody>
                {assignees.map((member) => (
                  <MemberRow key={member.id} member={member} t={t} />
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

interface MemberRowProps {
  member: RoleAssignee;
  t: (en: string, de: string) => string;
}

function MemberRow({ member, t }: MemberRowProps) {
  const currentRoleName = member.roleName
    ? roleLabel(member.roleName, t)
    : t("from their sign-in", "aus ihrer Anmeldung");

  return (
    <tr className="border-b border-border/50 hover:bg-muted/20">
      <td className="py-3">
        <Link to="/members/$memberId" params={{ memberId: member.id }} className="block hover:underline">
          <div className="font-medium">{member.displayName || member.subject}</div>
          {member.displayName && (
            <div className="text-xs text-muted-foreground">{member.subject}</div>
          )}
        </Link>
      </td>
      <td className="py-3 text-muted-foreground">{member.subject}</td>
      <td className="py-3">
        <span className="text-sm">{currentRoleName}</span>
      </td>
      <td className="py-3 text-right">
        {/* Role changes live on the detail page now -- it already has the full
            picture (admin toggle, exact role override, per-department seats),
            so this was a second, more confusing way to do the same thing. */}
        <Link
          to="/members/$memberId"
          params={{ memberId: member.id }}
          className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs transition hover:bg-muted"
        >
          {t("Open", "Öffnen")}
          <ChevronRight className="size-3" />
        </Link>
      </td>
    </tr>
  );
}
