import { Building2, ChevronDown, ChevronLeft, KeyRound, LogIn, Plus, Users } from "lucide-react";
import { useEffect, useState } from "react";
import {
  createDevTenant,
  listDevMembers,
  listDevTenants,
  loginDev,
  type DevMember,
  type DevTenant,
} from "@/lib/api";
import { roleLabel } from "@/components/app-shell";
import { useT } from "@/lib/i18n";

// The five built-in roles, in the ladder's own order (permissions.py). Each
// gets a STABLE synthetic subject (`dev-<role>`) rather than one random per
// click, so repeated quick-access logins keep answering as the same person
// instead of minting a fresh org_member row every time.
const BUILT_IN_ROLES = ["org_admin", "dept_manager", "operator", "auditor", "member"] as const;

function seatSummary(m: DevMember): string {
  return m.seats.map((s) => `${s.departmentName || s.departmentId} · ${s.seatRole}`).join(", ");
}

/** The whole dev sign-in flow: pick a company, then pick who you are in it.
 * Shown whenever `hasDevSession()` is false (routes/__root.tsx) -- there is
 * no silent fallback identity any more, so this is the ONLY door in. */
export function DevSignIn() {
  const t = useT();
  const [tenants, setTenants] = useState<DevTenant[]>([]);
  const [tenantsError, setTenantsError] = useState("");
  const [tenant, setTenant] = useState<DevTenant | null>(null);

  useEffect(() => {
    listDevTenants()
      .then(setTenants)
      .catch((e) => setTenantsError(e instanceof Error ? e.message : String(e)));
  }, []);

  return (
    <div className="grid min-h-screen place-items-center bg-background p-4">
      <main className="w-full max-w-xl rounded-xl border border-border bg-panel p-6 shadow-2xl">
        {tenant ? (
          <PersonaPicker tenant={tenant} onBack={() => setTenant(null)} />
        ) : (
          <TenantPicker
            tenants={tenants}
            error={tenantsError}
            onPick={setTenant}
            onCreated={setTenant}
          />
        )}
      </main>
    </div>
  );
}

function TenantPicker({
  tenants,
  error,
  onPick,
  onCreated,
}: {
  tenants: DevTenant[];
  error: string;
  onPick: (t: DevTenant) => void;
  onCreated: (t: DevTenant) => void;
}) {
  const t = useT();
  const [name, setName] = useState("Interior House");
  const [slug, setSlug] = useState("interior-house");
  const [busy, setBusy] = useState(false);
  const [createError, setCreateError] = useState("");

  async function create() {
    setBusy(true);
    setCreateError("");
    try {
      const created = await createDevTenant({ name, slug, region: "eu" });
      // A brand-new tenant has no members yet -- the persona picker's
      // built-in-role quick access is the only way in, which is correct: a
      // fresh company starts with nothing configured (§0, the two-person
      // company story).
      onCreated(created);
    } catch (e) {
      setCreateError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  return (
    <>
      <div className="flex items-start gap-3">
        <div className="grid h-10 w-10 place-items-center rounded-lg bg-primary/15 text-primary">
          <Building2 className="h-5 w-5" />
        </div>
        <div>
          <h1 className="font-serif text-2xl">{t("Sign in", "Anmelden")}</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {t(
              "Choose a company, or create a safely isolated one for local testing.",
              "Wähle eine Firma oder lege eine isolierte Testfirma an.",
            )}
          </p>
        </div>
      </div>
      <div className="mt-6 space-y-2">
        {tenants.map((tn) => (
          <button
            key={tn.id}
            onClick={() => onPick(tn)}
            className="flex w-full items-center justify-between rounded-md border border-border bg-background/30 px-4 py-3 text-left hover:border-primary/50"
          >
            <span>
              <span className="block font-medium">{tn.name}</span>
              <span className="font-mono text-xs text-muted-foreground">
                {tn.slug} · {tn.region}
              </span>
            </span>
            <LogIn className="h-4 w-4 text-primary" />
          </button>
        ))}
      </div>
      <div className="mt-6 border-t border-border pt-5">
        <h2 className="font-medium">{t("Create a local company", "Lokale Firma anlegen")}</h2>
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          <input
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              setSlug(
                e.target.value
                  .toLowerCase()
                  .replace(/[^a-z0-9]+/g, "-")
                  .replace(/(^-|-$)/g, ""),
              );
            }}
            placeholder={t("Company name", "Firmenname")}
            className="rounded-md border border-border bg-background/40 px-3 py-2 text-sm"
          />
          <input
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
            placeholder="company-slug"
            className="rounded-md border border-border bg-background/40 px-3 py-2 font-mono text-sm"
          />
        </div>
        <button
          onClick={create}
          disabled={busy || !name.trim() || !slug.trim()}
          className="mt-3 inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground disabled:opacity-40"
        >
          <Plus className="h-4 w-4" />
          {t("Create company", "Firma anlegen")}
        </button>
      </div>
      {(error || createError) && (
        <p className="mt-3 text-sm text-[color:var(--status-error)]">{error || createError}</p>
      )}
      <p className="mt-5 text-[11px] text-muted-foreground">
        {t(
          "Development only. Production instances are set up once through POST /auth/setup, not this picker.",
          "Nur für die Entwicklung. Produktivinstanzen werden einmalig über POST /auth/setup eingerichtet, nicht über diesen Dialog.",
        )}
      </p>
    </>
  );
}

function PersonaPicker({ tenant, onBack }: { tenant: DevTenant; onBack: () => void }) {
  const t = useT();
  const [members, setMembers] = useState<DevMember[] | null>(null);
  const [membersError, setMembersError] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [manualSubject, setManualSubject] = useState("");
  const [manualRole, setManualRole] = useState("member");

  useEffect(() => {
    listDevMembers(tenant.id)
      .then(setMembers)
      .catch((e) => setMembersError(e instanceof Error ? e.message : String(e)));
  }, [tenant.id]);

  async function signInAs(subject: string, role?: string) {
    setBusy(true);
    setError("");
    try {
      await loginDev({ tenantId: tenant.id, subject, role });
      window.location.reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  return (
    <>
      <div className="flex items-start gap-3">
        <button
          onClick={onBack}
          className="grid h-10 w-10 shrink-0 place-items-center rounded-lg border border-border text-muted-foreground hover:text-foreground"
          aria-label={t("Change company", "Firma wechseln")}
          title={t("Change company", "Firma wechseln")}
        >
          <ChevronLeft className="h-5 w-5" />
        </button>
        <div>
          <h1 className="font-serif text-2xl">{tenant.name}</h1>
          <p className="mt-1 text-sm text-muted-foreground">{t("Sign in as…", "Anmelden als…")}</p>
        </div>
      </div>

      <div className="mt-6">
        <h2 className="flex items-center gap-1.5 text-sm font-medium text-muted-foreground">
          <Users className="h-3.5 w-3.5" />
          {t("People", "Personen")}
        </h2>
        {membersError && (
          <p className="mt-2 text-sm text-[color:var(--status-error)]">{membersError}</p>
        )}
        {members === null && !membersError && (
          <p className="mt-2 text-sm text-muted-foreground">{t("Loading…", "Lädt…")}</p>
        )}
        {members?.length === 0 && (
          <p className="mt-2 text-sm text-muted-foreground">
            {t(
              "Nobody has signed in to this company yet — use quick access below.",
              "In dieser Firma hat sich noch niemand angemeldet — nutze den Schnellzugriff unten.",
            )}
          </p>
        )}
        {members && members.length > 0 && (
          <div className="mt-2 space-y-1.5">
            {members.map((m) => {
              const summary = seatSummary(m);
              return (
                <button
                  key={m.id}
                  disabled={busy}
                  onClick={() => signInAs(m.subject, "member")}
                  className="flex w-full items-center justify-between rounded-md border border-border bg-background/30 px-4 py-2.5 text-left hover:border-primary/50 disabled:opacity-40"
                >
                  <span className="min-w-0">
                    <span className="block truncate font-medium">{m.displayName || m.subject}</span>
                    <span className="block truncate text-xs text-muted-foreground">
                      {[
                        m.roleName ? roleLabel(m.roleName, t) : "",
                        m.allDepartments ? t("all departments", "alle Abteilungen") : summary,
                      ]
                        .filter(Boolean)
                        .join(" · ") || t("no role, no seat", "keine Rolle, kein Sitz")}
                    </span>
                  </span>
                  <LogIn className="h-4 w-4 shrink-0 text-primary" />
                </button>
              );
            })}
          </div>
        )}
      </div>

      <div className="mt-6 border-t border-border pt-5">
        <h2 className="flex items-center gap-1.5 text-sm font-medium text-muted-foreground">
          <KeyRound className="h-3.5 w-3.5" />
          {t("Quick access — built-in roles", "Schnellzugriff — eingebaute Rollen")}
        </h2>
        <div className="mt-2 grid grid-cols-2 gap-1.5 sm:grid-cols-3">
          {BUILT_IN_ROLES.map((role) => (
            <button
              key={role}
              disabled={busy}
              onClick={() => signInAs(`dev-${role}`, role)}
              className="rounded-md border border-border bg-background/30 px-3 py-2 text-sm hover:border-primary/50 disabled:opacity-40"
            >
              {roleLabel(role, t)}
            </button>
          ))}
        </div>
      </div>

      <div className="mt-5 border-t border-border pt-4">
        <button
          onClick={() => setAdvanced((v) => !v)}
          className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        >
          <ChevronDown
            className={`h-3.5 w-3.5 transition-transform ${advanced ? "rotate-180" : ""}`}
          />
          {t("Advanced: sign in as a specific subject/role", "Erweitert: bestimmtes Subjekt/Rolle")}
        </button>
        {advanced && (
          <div className="mt-3 grid gap-2 sm:grid-cols-[1fr_1fr_auto]">
            <input
              value={manualSubject}
              onChange={(e) => setManualSubject(e.target.value)}
              placeholder={t("subject", "Subjekt")}
              className="rounded-md border border-border bg-background/40 px-3 py-2 font-mono text-sm"
            />
            <input
              value={manualRole}
              onChange={(e) => setManualRole(e.target.value)}
              placeholder="role"
              className="rounded-md border border-border bg-background/40 px-3 py-2 font-mono text-sm"
            />
            <button
              onClick={() => signInAs(manualSubject, manualRole)}
              disabled={busy || !manualSubject.trim()}
              className="rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground disabled:opacity-40"
            >
              {t("Continue", "Weiter")}
            </button>
          </div>
        )}
      </div>

      {error && <p className="mt-4 text-sm text-[color:var(--status-error)]">{error}</p>}
      <p className="mt-5 text-[11px] text-muted-foreground">
        {t(
          "Development only. A person's authority comes from their seats and any tenant role assigned to them, never from this screen.",
          "Nur für die Entwicklung. Die Berechtigung kommt aus Sitzen und einer zugewiesenen Rolle, nie von diesem Bildschirm.",
        )}
      </p>
    </>
  );
}
