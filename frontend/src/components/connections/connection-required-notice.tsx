import type { ConnectionRequiredInfo } from "@/lib/connections";
import { useT } from "@/lib/i18n";
import { providerName } from "./texts";

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
  const connectHref = `/connections?connect=${encodeURIComponent(value.integration)}`;
  let text: string;
  let link: { href: string; label: string } | null = {
    href: connectHref,
    label: t("Connect now →", "Jetzt verbinden →"),
  };
  switch (value.reason) {
    case "needs_reauth":
      text = t(
        `The connection to ${account.en} has expired.`,
        `Die Verbindung zu ${account.deDative} ist abgelaufen.`,
      );
      link = { href: connectHref, label: t("Reconnect now →", "Jetzt erneut verbinden →") };
      break;
    case "schedule_without_owner":
      text = t(
        "This schedule has no owner. Please save it once again.",
        "Zeitplan hat keinen Besitzer, bitte einmal neu speichern.",
      );
      link = null;
      break;
    case "no_acting_person":
      text = t(
        "Nobody is working with this run, so the agent cannot choose an account. Give the agent a fixed access.",
        "Niemand arbeitet mit diesem Lauf, daher kann der Agent kein Konto wählen. Legen Sie beim Agenten einen festen Zugang fest.",
      );
      link = null;
      break;
    case "binding_needs_confirmation":
      text = t(
        `This agent's access to ${value.label} must be confirmed again.`,
        `Der Zugang dieses Agenten zu ${value.label} muss erneut bestätigt werden.`,
      );
      link = agentId
        ? {
            href: `/agents/${encodeURIComponent(agentId)}?tab=guardrails`,
            label: t("Check access →", "Zugang prüfen →"),
          }
        : null;
      break;
    case "not_configured":
      text = t(
        `No access to ${value.label} is set up yet.`,
        `Für ${value.label} ist noch kein Zugang eingerichtet.`,
      );
      link = { href: connectHref, label: t("Set up now →", "Jetzt einrichten →") };
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
      {link && (
        <a
          href={link.href}
          className="font-medium underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          {link.label}
        </a>
      )}
    </div>
  );
}
