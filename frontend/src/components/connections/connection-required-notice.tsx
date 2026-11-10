import { Link } from "@tanstack/react-router";
import type { ConnectionRequiredInfo } from "@/lib/connections";
import { useT } from "@/lib/i18n";
import { providerName, SCHEDULE_CHANGED } from "./texts";

/** A tool call needed an account that is not there: say which, and link to
 *  where it gets fixed. Shown in the run detail and the Copilot's run
 *  activity. `agentId` lets a binding that needs confirmation link to the
 *  agent's access setting. */
export function ConnectionRequiredNotice({
  value,
  agentId,
}: {
  value?: ConnectionRequiredInfo | null;
  agentId?: string | null;
}) {
  const t = useT();
  if (!value) return null;
  const name = providerName(value.provider);
  const account = name
    ? { en: `your ${name} account`, de: `Ihr ${name}-Konto`, deDative: `Ihrem ${name}-Konto` }
    : {
        en: `your access to ${value.label}`,
        de: `Ihren Zugang zu ${value.label}`,
        deDative: `Ihrem Zugang zu ${value.label}`,
      };
  // Where the problem gets fixed: the connect assistant, or the agent's
  // access setting. A null target means nothing to link to.
  let target: "connect" | "agent" | null = "connect";
  let linkLabel = t("Connect now →", "Jetzt verbinden →");
  let text: string;
  switch (value.reason) {
    case "needs_reauth":
      text = t(
        `The connection to ${account.en} has expired.`,
        `Die Verbindung zu ${account.deDative} ist abgelaufen.`,
      );
      linkLabel = t("Reconnect now →", "Jetzt erneut verbinden →");
      break;
    case "schedule_without_owner":
      text = t(
        "This schedule has no owner. Please save it once again.",
        "Dieser Zeitplan hat keinen Besitzer. Bitte speichern Sie ihn einmal neu.",
      );
      target = null;
      break;
    case "schedule_needs_confirmation":
      text = t(SCHEDULE_CHANGED.en, SCHEDULE_CHANGED.de);
      target = null;
      break;
    case "trigger_without_person":
      text = t(
        "This trigger may use its creator's account only if the creator allows it in the trigger's settings.",
        "Dieser Auslöser darf das Konto der Person, die ihn angelegt hat, nur nutzen, wenn diese Person es in den Einstellungen des Auslösers erlaubt.",
      );
      target = null;
      break;
    case "no_acting_person":
      text = t(
        "This run has no acting person, so the agent cannot pick an account. Give the agent a fixed access.",
        "Niemand arbeitet mit diesem Lauf, daher kann der Agent kein Konto wählen. Legen Sie beim Agenten einen festen Zugang fest.",
      );
      target = null;
      break;
    case "binding_needs_confirmation":
      text = t(
        `This agent's access to ${value.label} must be confirmed again.`,
        `Der Zugang dieses Agenten zu ${value.label} muss erneut bestätigt werden.`,
      );
      target = agentId ? "agent" : null;
      linkLabel = t("Check access →", "Zugang prüfen →");
      break;
    case "personal_account_withheld":
      // A run that deliberately acts for nobody (e.g. a decision carrying
      // someone else's instruction) may not use a member's own account,
      // even through a fixed access. An organisation account would do.
      text = t(
        `This run acts for nobody, so it may not use the personal account in this agent's fixed access to ${value.label}.`,
        `Dieser Lauf handelt für niemanden und darf daher das persönliche Konto aus dem festen Zugang dieses Agenten zu ${value.label} nicht nutzen.`,
      );
      target = agentId ? "agent" : null;
      linkLabel = t("Check access →", "Zugang prüfen →");
      break;
    case "not_configured":
      text = t(
        `No access to ${value.label} is set up yet.`,
        `Für ${value.label} ist noch kein Zugang eingerichtet.`,
      );
      linkLabel = t("Set up now →", "Jetzt einrichten →");
      break;
    default:
      text = t(
        `For this task the agent needs ${account.en}.`,
        `Für diese Aufgabe braucht der Agent Zugriff auf ${account.de}.`,
      );
  }
  return (
    <div
      role="alert"
      className="flex flex-wrap items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm"
    >
      <span>{text}</span>
      {target === "connect" && (
        <Link to="/connections" search={{ connect: value.integration }} className={LINK}>
          {linkLabel}
        </Link>
      )}
      {target === "agent" && agentId && (
        // The agent page reads `?tab=` once on arrival (agents.$id.tsx).
        <Link
          to="/agents/$id"
          params={{ id: agentId }}
          search={{ tab: "guardrails" }}
          className={LINK}
        >
          {linkLabel}
        </Link>
      )}
    </div>
  );
}

const LINK =
  "font-medium underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2";
