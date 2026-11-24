import { Loader2 } from "lucide-react";
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  useConfirmPersonal,
  usePersonalConfirmation,
  type ConfirmationPolicy,
  type PersonalConfirmationDTO,
  type PersonalConfirmationSetupDTO,
} from "@/lib/hooks";
import { errorCode } from "@/lib/hire-error";
import { useT } from "@/lib/i18n";

type Tr = ReturnType<typeof useT>;
type Agent = PersonalConfirmationDTO["agents"][number];

const FIELDS = [
  "enabled",
  "read",
  "modify",
  "only",
  "limits",
  "approval_eur",
  "approval_actions",
  "conditions",
  "connection_id",
  "default_connection_id",
] as const;

function fieldLabel(t: Tr, field: string): string {
  switch (field) {
    case "enabled":
      return t("Enabled", "Aktiv");
    case "read":
      return t("Read", "Lesen");
    case "modify":
      return t("Modify", "Ändern");
    case "only":
      return t("Only these actions", "Nur diese Aktionen");
    case "limits":
      return t("Limits", "Limits");
    case "approval_eur":
      return t("Approval threshold (EUR)", "Freigabe-Schwelle (EUR)");
    case "approval_actions":
      return t("Actions needing approval", "Aktionen mit Freigabe");
    case "conditions":
      return t("Conditions", "Bedingungen");
    case "connection_id":
      return t("Login", "Zugang");
    case "default_connection_id":
      return t("Default login", "Standard-Zugang");
    case "frame_conditions":
      return t("Conditions (department)", "Bedingungen (Abteilung)");
    case "agent_conditions":
      return t("Conditions (agent)", "Bedingungen (Agent)");
    case "agent_approval_eur":
      return t("Approval threshold (agent, EUR)", "Freigabe-Schwelle (Agent, EUR)");
    case "frame_extra":
      return t("More settings (department)", "Weitere Einstellungen (Abteilung)");
    case "agent_extra":
      return t("More settings (agent)", "Weitere Einstellungen (Agent)");
    default:
      return field;
  }
}

function isEmpty(v: unknown): boolean {
  return v == null || (Array.isArray(v) && v.length === 0);
}

function show(t: Tr, field: string, v: unknown): string {
  if (isEmpty(v)) {
    return field === "only" ? t("everything", "alles") : "-";
  }
  if (typeof v === "boolean") return v ? t("yes", "ja") : t("no", "nein");
  if (field === "limits" && Array.isArray(v)) {
    return v
      .map((l: { tools?: string[]; max?: number; per?: string }) =>
        t("{max} per {per}: {tools}", "{max} pro {per}: {tools}")
          .replace("{max}", String(l.max))
          .replace("{per}", l.per === "hour" ? t("hour", "Stunde") : t("day", "Tag"))
          .replace("{tools}", () => (l.tools ?? []).join(", ")),
      )
      .join("; ");
  }
  if (Array.isArray(v) && v.every((x) => typeof x === "string")) return v.join(", ");
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function same(a: unknown, b: unknown): boolean {
  return (isEmpty(a) && isEmpty(b)) || JSON.stringify(a) === JSON.stringify(b);
}

function Changed({ children }: { children: React.ReactNode }) {
  return (
    <span data-testid="changed" className="rounded bg-amber-500/20 px-1 font-medium">
      {children}
    </span>
  );
}

/** One tool, before and after: the changed rows first and highlighted, the
 *  rest collapsed. */
function ToolDiff({
  toolKey,
  applied,
  pending,
}: {
  toolKey: string;
  applied: ConfirmationPolicy | null;
  pending: ConfirmationPolicy | null;
}) {
  const t = useT();
  const fields = [
    ...FIELDS,
    ...Object.keys({ ...applied, ...pending }).filter(
      (k) => !(FIELDS as readonly string[]).includes(k),
    ),
  ];
  const rows = fields.map((f) => ({
    f,
    before: applied?.[f],
    after: pending?.[f],
    changed: !same(applied?.[f], pending?.[f]),
  }));
  const diff = rows.filter((r) => r.changed);
  const rest = rows.filter((r) => !r.changed && !(isEmpty(r.before) && isEmpty(r.after)));
  const row = (r: (typeof rows)[number]) => (
    <tr key={r.f} className="align-top">
      <td className="py-0.5 pr-3 text-muted-foreground">{fieldLabel(t, r.f)}</td>
      <td className="py-0.5 pr-3">{show(t, r.f, r.before)}</td>
      <td className="py-0.5">
        {r.changed ? <Changed>{show(t, r.f, r.after)}</Changed> : show(t, r.f, r.after)}
      </td>
    </tr>
  );
  return (
    <li data-testid={`tool-${toolKey}`} className="rounded-md border border-border/60 p-2">
      <div className="text-sm font-medium">{toolKey}</div>
      {applied == null && (
        <p className="text-[11px] text-muted-foreground">
          {t("Nothing is granted for it now.", "Bisher ist dafür nichts freigegeben.")}
        </p>
      )}
      <table className="mt-1 w-full text-left text-xs">
        <thead className="text-[10px] uppercase tracking-widest text-muted-foreground">
          <tr>
            <th className="pr-3 font-normal" />
            <th className="pr-3 font-normal">{t("Now", "Jetzt")}</th>
            <th className="font-normal">{t("After you confirm", "Nach deiner Bestätigung")}</th>
          </tr>
        </thead>
        <tbody>{diff.map(row)}</tbody>
      </table>
      {rest.length > 0 && (
        <details className="mt-1 text-xs">
          <summary className="cursor-pointer text-muted-foreground">
            {t("Unchanged settings", "Unveränderte Einstellungen")}
          </summary>
          <table className="mt-1 w-full text-left">
            <tbody>{rest.map(row)}</tbody>
          </table>
        </details>
      )}
    </li>
  );
}

function changeLabel(t: Tr, c: string): string {
  switch (c) {
    case "core":
      return t("version, setup or goal", "Version, Einrichtung oder Ziel");
    case "frame":
      return t("department frame", "Rahmen der Abteilung");
    case "tools":
      return t("tools", "Werkzeuge");
    case "skills":
      return t("skills", "Fähigkeiten");
    case "threshold":
      return t("approval threshold", "Freigabe-Schwelle");
    default:
      return c;
  }
}

function SetupView({ s }: { s: PersonalConfirmationSetupDTO }) {
  const t = useT();
  const scopes = Array.isArray(s.scopes) ? s.scopes.join(", ") : s.scopes ? String(s.scopes) : "";
  return (
    <li className="text-xs">
      <span className="font-medium">{s.tool}</span>
      <dl className="mt-0.5 grid grid-cols-[auto_1fr] gap-x-3 text-muted-foreground">
        {s.serverUrl && (
          <>
            <dt>{t("Server", "Server")}</dt>
            <dd className="break-all text-foreground">{s.serverUrl}</dd>
          </>
        )}
        {s.transport && (
          <>
            <dt>{t("Transport", "Transport")}</dt>
            <dd className="text-foreground">{s.transport}</dd>
          </>
        )}
        {scopes && (
          <>
            <dt>{t("Scopes", "Berechtigungen")}</dt>
            <dd className="text-foreground">{scopes}</dd>
          </>
        )}
        {s.configKeys.length > 0 && (
          <>
            <dt>{t("Settings", "Einstellungen")}</dt>
            <dd className="text-foreground">{s.configKeys.join(", ")}</dd>
          </>
        )}
        {s.logins.length > 0 && (
          <>
            <dt>{t("Logins", "Zugänge")}</dt>
            <dd className="text-foreground">
              {s.logins
                .map((l) => (l.ownedByOwner ? `${l.name} (${t("yours", "deiner")})` : l.name))
                .join(", ")}
            </dd>
          </>
        )}
      </dl>
    </li>
  );
}

/** One tool of an agent's grant: every key it carries, nothing left out. */
function GrantEntry({ toolKey, p }: { toolKey: string; p: ConfirmationPolicy }) {
  const t = useT();
  const off = p.enabled === false;
  const keys = Object.keys(p).filter((k) => k !== "enabled" && k !== "unreadable");
  return (
    <li data-testid={`agent-tool-${toolKey}`}>
      <span className="font-medium">{toolKey}</span>{" "}
      <span className="text-muted-foreground">{off ? t("off", "aus") : t("on", "an")}</span>
      {typeof p.unreadable === "string" && (
        <p role="alert" className="text-destructive">
          {t(
            "This tool's settings can't be read, so it stays blocked until an administrator fixes them: {raw}",
            "Die Einstellungen dieses Werkzeugs sind nicht lesbar, daher bleibt es gesperrt, bis eine Administration sie korrigiert: {raw}",
          ).replace("{raw}", () => String(p.unreadable))}
        </p>
      )}
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 text-muted-foreground">
        {keys
          .filter((k) => !isEmpty(p[k]) || p[k] === false)
          .map((k) => (
            <div key={k} className="contents">
              <dt>{fieldLabel(t, k)}</dt>
              <dd className="break-words text-foreground">{show(t, k, p[k])}</dd>
            </div>
          ))}
      </dl>
    </li>
  );
}

function AgentCard({ a }: { a: Agent }) {
  const t = useT();
  const hasThreshold = a.approvalThreshold != null && a.approvalThreshold !== "";
  return (
    <li className="rounded-md border border-border/60 p-2">
      <div className="text-sm font-medium">{a.name}</div>
      <p className="text-xs text-muted-foreground">
        {t("Changed: {what}", "Geändert: {what}").replace(
          "{what}",
          a.changes.map((c) => changeLabel(t, c)).join(", "),
        )}
      </p>
      <p className="text-xs">
        {t("Approval threshold: {value}", "Freigabe-Schwelle: {value}").replace(
          "{value}",
          hasThreshold ? `${a.approvalThreshold} EUR` : t("none", "keine"),
        )}
      </p>
      {a.changes.includes("core") && a.setup && a.setup.length > 0 && (
        <details className="mt-1">
          <summary className="cursor-pointer text-xs text-muted-foreground">
            {t("Setup it uses", "Eingesetzte Einrichtung")}
          </summary>
          <ul className="mt-1 space-y-1.5">
            {a.setup.map((s) => (
              <SetupView key={s.tool} s={s} />
            ))}
          </ul>
        </details>
      )}
      {a.tools && Object.keys(a.tools).length > 0 && (
        <details
          data-testid={`agent-tools-${a.agentId}`}
          open={a.changes.includes("tools") || a.changes.includes("threshold")}
          className="mt-1"
        >
          <summary className="cursor-pointer text-xs text-muted-foreground">
            {t("Tools it may use", "Werkzeuge, die er nutzen darf")}
          </summary>
          <ul className="mt-1 space-y-1.5 text-xs">
            {Object.entries(a.tools).map(([key, p]) => (
              <GrantEntry key={key} toolKey={key} p={p} />
            ))}
          </ul>
        </details>
      )}
    </li>
  );
}

/** Owner's card in the profile: what an administrator changed in their
 *  personal department and waits for their confirmation, in content. */
export function PersonalConfirmationNotice({ enabled = true }: { enabled?: boolean }) {
  const t = useT();
  const qc = useQueryClient();
  const query = usePersonalConfirmation(enabled);
  const confirm = useConfirmPersonal();
  // A refusal belongs to the summary it answered; the "review again" note
  // stays until the owner confirms again.
  const [note, setNote] = useState<{ text: string; token: string; keep: boolean } | null>(null);

  const data = query.data;
  if (!data || typeof data !== "object" || !data.needed) return null;

  const onConfirm = () => {
    setNote(null);
    confirm.mutate(data.token, {
      onError: (err) => {
        const status = (err as { status?: number }).status;
        if (status === 409 || errorCode(err) === "confirmation_changed") {
          void qc.invalidateQueries({ queryKey: ["personal-department", "confirmation"] });
          setNote({
            text: t(
              "Something changed meanwhile. Please review again.",
              "In der Zwischenzeit hat sich etwas geändert. Bitte erneut prüfen.",
            ),
            token: data.token,
            keep: true,
          });
        } else {
          const code = errorCode(err);
          setNote({
            token: data.token,
            keep: false,
            text: t(
              "This could not be confirmed: {reason}",
              "Das ließ sich nicht bestätigen: {reason}",
            ).replace(
              "{reason}",
              () =>
                code ??
                (err instanceof Error ? err.message : t("unknown error", "unbekannter Fehler")),
            ),
          });
        }
      },
    });
  };

  return (
    <section
      aria-label={t("Waiting for your confirmation", "Wartet auf deine Bestätigung")}
      className="mt-3 rounded-md border border-amber-500/40 bg-amber-500/5 p-3"
    >
      <h3 className="text-sm font-medium">
        {t("Waiting for your confirmation", "Wartet auf deine Bestätigung")}
      </h3>
      <p className="mt-1 text-xs text-muted-foreground">
        {t(
          "An administrator changed your personal department. Nothing below applies until you confirm it.",
          "Eine Administration hat deine persönliche Abteilung geändert. Nichts davon gilt, bis du es bestätigst.",
        )}
      </p>

      {data.tools.length > 0 && (
        <div className="mt-3">
          <h4 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {t("Tools", "Werkzeuge")}
          </h4>
          <ul className="mt-1 space-y-2">
            {data.tools.map((x) => (
              <ToolDiff key={x.key} toolKey={x.key} applied={x.applied} pending={x.pending} />
            ))}
          </ul>
        </div>
      )}

      {data.skills.length > 0 && (
        <div className="mt-3">
          <h4 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {t("Skills", "Fähigkeiten")}
          </h4>
          <ul className="mt-1 space-y-1">
            {data.skills.map((s) => (
              <li key={s.name} className="text-xs">
                <span className="text-sm font-medium">{s.name}</span>{" "}
                <span
                  data-testid="skill-status"
                  className="rounded-full border border-border px-1.5 text-[10px]"
                >
                  {s.status === "new"
                    ? t("new", "neu")
                    : s.status === "changed"
                      ? t("changed", "geändert")
                      : s.status}
                </span>
                {s.description && <div className="text-muted-foreground">{s.description}</div>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {data.agents.length > 0 && (
        <div className="mt-3">
          <h4 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {t("Assistants", "Assistenten")}
          </h4>
          <ul className="mt-1 space-y-2">
            {data.agents.map((a) => (
              <AgentCard key={a.agentId} a={a} />
            ))}
          </ul>
        </div>
      )}

      {data.department && (
        <details className="mt-3" open>
          <summary className="cursor-pointer text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {t("Department", "Abteilung")}
          </summary>
          <p className="mt-1 text-xs text-muted-foreground">{t("Goal", "Ziel")}</p>
          <p className="whitespace-pre-wrap text-sm">{data.department.goal}</p>
          {data.department.frameCapaDefaults &&
            Object.keys(data.department.frameCapaDefaults).length > 0 && (
              <>
                <p className="mt-2 text-xs text-muted-foreground">
                  {t("Tools reset to", "Werkzeuge werden zurückgesetzt auf")}
                </p>
                <pre className="mt-1 overflow-x-auto rounded bg-muted/40 p-2 text-[11px]">
                  {JSON.stringify(data.department.frameCapaDefaults, null, 2)}
                </pre>
              </>
            )}
          {Object.keys(data.department.frame).length > 0 && (
            <pre className="mt-1 overflow-x-auto rounded bg-muted/40 p-2 text-[11px]">
              {JSON.stringify(data.department.frame, null, 2)}
            </pre>
          )}
        </details>
      )}

      {note && (note.keep || note.token === data.token) && (
        <p role="alert" className="mt-3 text-xs text-destructive">
          {note.text}
        </p>
      )}
      <button
        type="button"
        onClick={onConfirm}
        disabled={confirm.isPending}
        className="mt-3 inline-flex h-8 items-center gap-1.5 rounded-md bg-primary px-3 text-xs font-medium text-primary-foreground disabled:opacity-50"
      >
        {confirm.isPending && <Loader2 className="h-3 w-3 animate-spin" />}
        {t("Review and confirm", "Prüfen und bestätigen")}
      </button>
    </section>
  );
}
