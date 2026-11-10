import { ApiError } from "@/lib/api";
import type { Connection, CredentialCheck, Integration, OAuthErrorCode } from "@/lib/connections";

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
