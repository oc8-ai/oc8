import { ApiError } from "@/lib/api";
import type {
  Connection,
  CredentialCheck,
  CredentialUsedBy,
  EffectiveLogin,
  Integration,
  OAuthErrorCode,
} from "@/lib/connections";

type T = (en: string, de: string) => string;

/** A schedule whose agent changed since its creator last saved it no longer
 *  acts through their accounts (`schedule_needs_confirmation`). */
export const SCHEDULE_CHANGED = {
  en: "The agent was changed. Please open and save the schedule again so it works on your behalf again.",
  de: "Der Agent wurde geändert. Bitte öffne und speicher den Zeitplan erneut, damit er wieder in deinem Namen arbeitet.",
} as const;

export function providerName(provider: string | null | undefined): string {
  if (provider === "google") return "Google";
  if (provider === "microsoft") return "Microsoft";
  return "";
}

/** What oc8 can do with the account, in plain words (assistant step 1). */
export function benefits(provider: string | null, t: T): string[] {
  if (provider === "google") {
    return [
      t("Send e-mail from Gmail", "E-Mails aus Gmail senden"),
      t("Plan appointments in Google Calendar", "Termine im Google Kalender planen"),
      t(
        "Work with the files it creates in Google Drive",
        "Mit den Dateien arbeiten, die es in Google Drive anlegt",
      ),
    ];
  }
  return [
    t(
      "Read, sort and send your e-mail in Outlook",
      "E-Mails in Outlook lesen, sortieren und senden",
    ),
    t("Plan appointments in your calendar", "Termine in deinem Kalender planen"),
    t(
      "Work with files in OneDrive and SharePoint",
      "Mit Dateien in OneDrive und SharePoint arbeiten",
    ),
  ];
}

export function oneLineBenefit(integration: Integration, t: T): string {
  return integration.kind === "oauth"
    ? t("E-mail, calendar and files", "E-Mails, Kalender und Dateien")
    : t(
        `Records and tasks in ${integration.label}`,
        `Datensätze und Aufgaben in ${integration.label}`,
      );
}

export function checkErrorText(check: CredentialCheck, t: T): string {
  if (check.reason === "auth") {
    return t(
      "The username or API key is not correct.",
      "Benutzername oder API-Schlüssel stimmen nicht.",
    );
  }
  if (check.reason === "unreachable") {
    return t(
      "The server cannot be reached. Please check the address.",
      "Der Server ist nicht erreichbar. Bitte prüf die Adresse.",
    );
  }
  return t(
    "The connection did not work. Please check your entries and try again.",
    "Die Verbindung hat nicht geklappt. Bitte prüf deine Eingaben und versuch es erneut.",
  );
}

/** The `?error=` the OAuth callback (or a failed completion) reports on /connections. */
export function returnErrorText(code: string, t: T): string {
  switch (code as OAuthErrorCode) {
    case "admin_consent_required":
      return t(
        "Your IT administrator must approve this connection first. Please send them the link below.",
        "Dein IT-Administrator muss diese Verbindung zuerst freigeben. Bitte sende ihm den Link unten.",
      );
    case "tenant_mismatch":
      return t(
        "This account belongs to a different organisation than the one connected before. Please sign in with the right account.",
        "Dieses Konto gehört zu einer anderen Organisation als die zuvor verbundene. Bitte melde dich mit dem richtigen Konto an.",
      );
    case "account_unresolved":
      return t(
        "We could not tell which account you signed in with. Please try again.",
        "Wir konnten nicht erkennen, mit welchem Konto du dich angemeldet hast. Bitte versuch es erneut.",
      );
    case "not_permitted":
      return t(
        "You are not allowed to connect this account for the organisation.",
        "Du darfst dieses Konto nicht für die Organisation verbinden.",
      );
    case "account_conflict":
      return t(
        "This account is already connected by someone else.",
        "Dieses Konto ist bereits von jemand anderem verbunden.",
      );
    case "consent_required":
      return t(
        "The permissions were not granted. Please sign in again and allow access.",
        "Die Berechtigungen wurden nicht erteilt. Bitte melde dich erneut an und erlaube den Zugriff.",
      );
    case "access_denied":
      return t(
        "You cancelled the sign-in. Nothing was connected.",
        "Du hast die Anmeldung abgebrochen. Es wurde nichts verbunden.",
      );
    case "interaction_required":
      return t(
        "The provider needs you to sign in again. Please try once more.",
        "Der Anbieter verlangt eine erneute Anmeldung. Bitte versuch es noch einmal.",
      );
    case "exchange_failed":
    case "provider_error":
      return t(
        "Connecting did not work. Please try again.",
        "Das Verbinden hat nicht geklappt. Bitte versuch es erneut.",
      );
    default:
      if (code === "member_gone") {
        return t(
          "Your account in this organisation no longer exists.",
          "Dein Konto in dieser Organisation besteht nicht mehr.",
        );
      }
      return t(
        "Connecting did not work. Please try again.",
        "Das Verbinden hat nicht geklappt. Bitte versuch es erneut.",
      );
  }
}

export function startErrorText(err: unknown, t: T): string {
  if (
    err instanceof ApiError &&
    err.status === 400 &&
    /oauth client configured/i.test(err.message)
  ) {
    return t(
      "This connection is not set up yet. Please contact your administrator.",
      "Diese Verbindung ist noch nicht eingerichtet. Bitte wende dich an deinen Administrator.",
    );
  }
  return t(
    "Connecting could not start. Please try again.",
    "Das Verbinden konnte nicht starten. Bitte versuch es erneut.",
  );
}

/** A failed save or check, in plain words; never the backend's own text. */
export function saveErrorText(err: unknown, t: T, action: "save" | "check" = "save"): string {
  if (err instanceof ApiError) {
    if (err.status === 403) {
      return t("You are not allowed to do this.", "Du hast dafür keine Berechtigung.");
    }
    if (err.status === 409) {
      return t("This access already exists.", "Dieser Zugang existiert bereits.");
    }
    if (err.status === 422) {
      return t("Please check your entries.", "Bitte prüf deine Eingaben.");
    }
  }
  return action === "check"
    ? t(
        "The check failed. Please try again.",
        "Die Prüfung ist fehlgeschlagen. Bitte versuch es erneut.",
      )
    : t(
        "Saving failed. Please try again.",
        "Das Speichern ist fehlgeschlagen. Bitte versuch es erneut.",
      );
}

/** A connection that no longer works and needs its owner to sign in again. */
export function isBroken(c: Connection): boolean {
  return c.kind === "oauth" ? c.status !== "active" : c.status === "failed";
}

/** "8. Nov." / "8 Nov" -- the short date the connection lists show. */
export function shortDate(iso: string, lang: string): string {
  return new Date(iso).toLocaleDateString(lang === "de" ? "de-DE" : "en-GB", {
    day: "numeric",
    month: "short",
  });
}

/** `text` with each `{key}` replaced by `vars[key]`. The `{…}` placeholders
 *  keep the English text a literal, so the translation catalogs carry it. */
export function fill(text: string, vars: Record<string, string>): string {
  return text.replace(/\{(\w+)\}/g, (whole, key: string) => vars[key] ?? whole);
}

/** "{name} is incomplete: {fields} missing", with the fields' labels. */
export function incompleteText(t: T, name: string, fields: readonly string[]): string {
  return fill(
    t("{name} is incomplete: {fields} missing", "{name} ist unvollständig: {fields} fehlt"),
    {
      name,
      fields: fields.join(", "),
    },
  );
}

/** "used by N agents" for a login's option or row, or "" when none. */
export function usedByAgentsText(t: T, agents: number): string {
  if (agents <= 0) return "";
  return agents === 1
    ? t("used by 1 agent", "von 1 Agenten genutzt")
    : fill(t("used by {count} agents", "von {count} Agenten genutzt"), { count: String(agents) });
}

/** The line under an agent's tool: which login its chats use, and why --
 *  or why none can be used. `label` turns a field key into its label. */
export function effectiveLoginText(
  login: EffectiveLogin,
  t: T,
  label: (key: string) => string = (key) => key,
): string {
  const name = login.credentialName ?? "";
  if (login.reason === "incomplete") {
    return incompleteText(t, name, login.missing.map(label));
  }
  if (login.reason === "ambiguous") {
    return t(
      "Several logins are enabled — choose one for this agent",
      "Mehrere Zugänge freigegeben – wähle einen für diesen Agenten",
    );
  }
  if (login.reason === "not_connected") {
    return t(
      "Uses the login of the person in the chat — you have none yet",
      "Nutzt den Zugang der Person im Chat – du hast noch keinen",
    );
  }
  if (login.reason === "binding_needs_confirmation") {
    return t(
      "The login chosen for this agent is not confirmed — runs refuse it",
      "Der für diesen Agenten gewählte Zugang ist nicht bestätigt – Läufe lehnen ihn ab",
    );
  }
  if (login.reason === "needs_reauth") {
    return t(
      "The chosen account must be connected again",
      "Das gewählte Konto muss erneut verbunden werden",
    );
  }
  if (login.reason === "not_configured" || (!login.reason && !login.source)) {
    return t("No login set up yet", "Noch kein Zugang eingerichtet");
  }
  if (login.reason) {
    return t(
      "This tool cannot use a login in your chats right now",
      "Dieses Werkzeug kann in deinen Chats gerade keinen Zugang nutzen",
    );
  }
  switch (login.source) {
    case "agent":
      return fill(
        t("Uses {name} — chosen for this agent", "Nutzt {name} – für diesen Agenten gewählt"),
        { name },
      );
    case "own":
      return fill(
        t(
          "Uses your own login {name} in your chats",
          "Nutzt in deinen Chats deinen eigenen Zugang {name}",
        ),
        { name },
      );
    case "department":
      return fill(
        t(
          "Uses {name} — enabled for the department",
          "Nutzt {name} – für die Abteilung freigegeben",
        ),
        { name },
      );
    case "organisation":
      return fill(
        t("Uses {name} — the organisation's login", "Nutzt {name} – den Zugang der Organisation"),
        { name },
      );
    default:
      return name
        ? fill(
            t(
              "Uses {name} — set up for the department before logins were shared",
              "Nutzt {name} – für die Abteilung eingerichtet, bevor Zugänge geteilt wurden",
            ),
            { name },
          )
        : t("Uses the department's earlier setup", "Nutzt die frühere Einrichtung der Abteilung");
  }
}

/** "Used by 2 agents · enabled for 3 departments", or "" when unused. */
export function usedByText(t: T, usedBy: CredentialUsedBy | undefined): string {
  const agents = usedByAgentsText(t, usedBy?.agents.length ?? 0);
  const departments = usedBy?.departments.length ?? 0;
  return [
    agents,
    departments === 1
      ? t("used in 1 department", "in 1 Abteilung genutzt")
      : departments > 1
        ? fill(t("used in {count} departments", "in {count} Abteilungen genutzt"), {
            count: String(departments),
          })
        : "",
  ]
    .filter(Boolean)
    .join(" · ");
}

/** Why a credential cannot be deleted while data sources read it (the
 *  server refuses even a forced delete), or null when none does. */
export function dataSourceBlockText(
  t: T,
  name: string,
  usedBy: CredentialUsedBy | undefined,
): string | null {
  const sources = usedBy?.dataSources ?? [];
  if (!sources.length) return null;
  return fill(
    sources.length === 1
      ? t(
          "{name} is still read by the data source {sources}. Choose another credential there first, then delete it.",
          "{name} wird noch von der Datenquelle {sources} gelesen. Wähl dort zuerst andere Zugangsdaten und lösch sie dann.",
        )
      : t(
          "{name} is still read by the data sources {sources}. Choose another credential there first, then delete it.",
          "{name} wird noch von den Datenquellen {sources} gelesen. Wähl dort zuerst andere Zugangsdaten und lösch sie dann.",
        ),
    { name, sources: sources.join(", ") },
  );
}

/** What deleting a login still in use means, for the confirmation after the
 *  server's 409: agents that chose it are left without one (never switched to
 *  another), departments lose it. */
export function inUseDescription(t: T, name: string, usedBy: CredentialUsedBy | undefined) {
  const what = usedByText(t, usedBy);
  return fill(
    t(
      "{name} is still in use ({what}). Delete it anyway? Agents that chose it then have no login until someone chooses another.",
      "{name} wird noch genutzt ({what}). Trotzdem löschen? Agenten, die ihn gewählt haben, haben dann keinen Zugang, bis jemand einen anderen wählt.",
    ),
    { name, what: what || t("by a setup", "von einer Einrichtung") },
  );
}
