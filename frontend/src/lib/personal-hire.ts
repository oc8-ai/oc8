import { ApiError } from "@/lib/api";
import { errorCode } from "@/lib/hire-error";
import type { Connection } from "@/lib/connections";
import type { PersonalTemplateDTO } from "@/lib/hooks";
import type { useT } from "@/lib/i18n";

type T = ReturnType<typeof useT>;

/** A readable reason for a refused hire, by the server's machine code. */
export function hireErrorText(err: unknown, t: T): string {
  const code = errorCode(err);
  switch (code) {
    case "personal_agent_limit":
      return t(
        "You've reached the maximum number of personal assistants",
        "Du hast die maximale Anzahl persönlicher Assistenten erreicht",
      );
    case "personal_departments_disabled":
      return t("Personal assistants are turned off", "Persönliche Assistenten sind ausgeschaltet");
    case "template_not_personal":
      return t("This assistant is not available", "Dieser Assistent ist nicht verfügbar");
    case "owner_left":
      return t(
        "Your account can no longer hire assistants",
        "Dein Konto kann keine Assistenten mehr einstellen",
      );
    case "hire_changed":
      return t(
        "This assistant changed while you were looking at it. Please review it again.",
        "Dieser Assistent hat sich geändert, während du ihn angesehen hast. Bitte prüfe ihn erneut.",
      );
    case "connection_required":
      return t("Choose a login for this assistant.", "Wähle einen Zugang für diesen Assistenten.");
    case "connection_not_offered":
      return t(
        "This assistant does not use that login.",
        "Dieser Assistent verwendet diesen Zugang nicht.",
      );
    case "connection_unavailable":
      return t(
        "That service is not available in your organisation.",
        "Dieser Dienst ist in deiner Organisation nicht verfügbar.",
      );
    case "connections_not_declared":
      return t(
        "This assistant does not use any login.",
        "Dieser Assistent verwendet keinen Zugang.",
      );
    case "login_not_usable":
      return t(
        "That login can't be used. Choose one of your own logins.",
        "Dieser Zugang kann nicht verwendet werden. Wähle einen deiner eigenen Zugänge.",
      );
    case "login_needs_reauth":
      return t(
        "That login has expired. Connect it again or choose another.",
        "Dieser Zugang ist abgelaufen. Verbinde ihn erneut oder wähle einen anderen.",
      );
    case "credential_incomplete":
      return t(
        "That login is missing details. Complete it or choose another.",
        "Diesem Zugang fehlen Angaben. Vervollständige ihn oder wähle einen anderen.",
      );
    case "confirm_share_required":
      return t(
        "Please confirm that the assistant may use your account.",
        "Bitte bestätige, dass der Assistent dein Konto nutzen darf.",
      );
  }
  if (err instanceof ApiError && err.status === 404)
    return t("This assistant is not available", "Dieser Assistent ist nicht verfügbar");
  if (err instanceof ApiError && err.status === 400)
    return t(
      "This assistant isn't allowed by your organisation's rules",
      "Dieser Assistent ist nach den Regeln deiner Organisation nicht erlaubt",
    );
  return t("Couldn't hire the assistant", "Assistent konnte nicht eingestellt werden");
}

/** Whether the hire needs the dialog: the member consents to what the
 *  assistant gets (any tool or skill) and chooses its logins. Only a template
 *  that connects to nothing and grants nothing hires in one click. */
export function needsHireDialog(tpl: PersonalTemplateDTO): boolean {
  return (
    tpl.connectionsAny.length > 0 ||
    tpl.connectionsOptional.length > 0 ||
    tpl.grants.tools.length > 0 ||
    tpl.grants.skills.length > 0
  );
}

/** The member's own logins of `integration` a hire may bind: OAuth that is
 *  active (not device-code), credentials neither failed nor incomplete. */
export function usableLogins(mine: Connection[], integration: string): Connection[] {
  return mine.filter(
    (c) =>
      c.owner === "me" &&
      c.integration === integration &&
      (c.kind === "oauth"
        ? c.status === "active" && c.grantType !== "device_code"
        : c.status !== "failed" && !(c.incomplete && c.incomplete.length > 0)),
  );
}
