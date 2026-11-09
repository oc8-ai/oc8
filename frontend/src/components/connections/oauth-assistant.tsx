import { toast } from "sonner";
import { type Integration, type Owner, useStartOAuth } from "@/lib/connections";
import { useT } from "@/lib/i18n";
import { benefits, providerName, startErrorText } from "./texts";

const BUTTON =
  "inline-flex min-h-11 items-center rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-50";

export function OAuthAssistant({
  integration,
  owner,
  redirect = (url: string) => window.location.assign(url),
}: {
  integration: Integration;
  owner: Owner;
  redirect?: (url: string) => void;
}) {
  const t = useT();
  const start = useStartOAuth();
  const provider = integration.provider ?? "microsoft";
  const name = providerName(provider);

  const signIn = async () => {
    try {
      const { authorizationUrl } = await start.mutateAsync({ provider, owner });
      redirect(authorizationUrl);
    } catch (err) {
      toast.error(startErrorText(err, t));
    }
  };

  return (
    <div className="space-y-4">
      <p className="text-sm">
        {owner === "organisation"
          ? t(
              `oc8 can then work with this ${name} account for the whole organisation:`,
              `oc8 kann danach für die ganze Organisation mit diesem ${name}-Konto arbeiten:`,
            )
          : t(
              `oc8 can then work with your ${name} account:`,
              `oc8 kann danach mit Ihrem ${name}-Konto arbeiten:`,
            )}
      </p>
      <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
        {benefits(provider, t).map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
      <p className="text-xs text-muted-foreground">
        {t(
          `Your password stays with ${name}. You can disconnect at any time.`,
          `Ihr Passwort bleibt bei ${name}. Sie können die Verbindung jederzeit trennen.`,
        )}
      </p>
      <button type="button" onClick={signIn} disabled={start.isPending} className={BUTTON}>
        {t(`Sign in with ${name}`, `Mit ${name} anmelden`)}
      </button>
    </div>
  );
}
