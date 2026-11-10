// The connect assistant for one integration, in a dialog: sign-in for
// Microsoft/Google, a short form for Odoo/HubSpot/Jira. Exported for other
// pages and editions (onboarding cards, integration pages).
import { useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { type Owner, useIntegrations } from "@/lib/connections";
import { useMay } from "@/lib/governance-hooks";
import { useT } from "@/lib/i18n";
import { CredentialAssistant } from "./credential-assistant";
import { IntegrationIcon } from "./integration-icon";
import { OAuthAssistant } from "./oauth-assistant";
import { oneLineBenefit } from "./texts";

export interface ConnectDialogProps {
  /** The integration's name (the plugin name, e.g. "microsoft365", "odoo_mcp"). */
  integration: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  defaultOwner?: Owner;
  /** Edit an organisation access's values instead of creating one. */
  editCredentialId?: string;
  /** Where the browser goes for the provider's sign-in (tests inject it). */
  redirect?: (url: string) => void;
}

export function ConnectDialog({
  integration: name,
  open,
  onOpenChange,
  defaultOwner = "me",
  editCredentialId,
  redirect,
}: ConnectDialogProps) {
  const t = useT();
  const may = useMay();
  const mayChooseOwner = may("integration:manage") && !editCredentialId;
  const [chosen, setChosen] = useState<Owner | null>(null);
  const owner: Owner = mayChooseOwner ? (chosen ?? defaultOwner) : "me";
  const setOwner = setChosen;
  const { data: integrations = [], isLoading, isError } = useIntegrations();
  const integration = integrations.find((i) => i.name === name);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            {integration && <IntegrationIcon integration={integration} />}
            {integration
              ? t(`Connect ${integration.label}`, `${integration.label} verbinden`)
              : t("Connect", "Verbinden")}
          </DialogTitle>
          <DialogDescription>
            {integration
              ? oneLineBenefit(integration, t)
              : t(
                  "Connect an account to use it in oc8.",
                  "Verbinde ein Konto, um es in oc8 zu nutzen.",
                )}
          </DialogDescription>
        </DialogHeader>
        {isLoading ? null : isError ? (
          <p role="alert" className="text-sm text-destructive">
            {t(
              "The integrations could not be loaded. Please try again later.",
              "Die Integrationen konnten nicht geladen werden. Bitte versuch es später erneut.",
            )}
          </p>
        ) : !integration || !integration.available ? (
          <p className="text-sm text-muted-foreground">
            {t(
              "This integration is not available yet.",
              "Diese Integration ist noch nicht verfügbar.",
            )}
          </p>
        ) : (
          <div className="space-y-4">
            {mayChooseOwner && (
              <fieldset className="flex flex-wrap gap-4 text-sm">
                <legend className="sr-only">
                  {t("Whose access is this?", "Wessen Zugang ist das?")}
                </legend>
                <label className="inline-flex min-h-11 items-center gap-2">
                  <input
                    type="radio"
                    name="connect-owner"
                    checked={owner === "me"}
                    onChange={() => setOwner("me")}
                  />
                  {t("Only for me", "Nur für mich")}
                </label>
                <label className="inline-flex min-h-11 items-center gap-2">
                  <input
                    type="radio"
                    name="connect-owner"
                    checked={owner === "organisation"}
                    onChange={() => setOwner("organisation")}
                  />
                  {t("For the organisation", "Für die Organisation")}
                </label>
              </fieldset>
            )}
            {integration.kind === "oauth" ? (
              <OAuthAssistant integration={integration} owner={owner} redirect={redirect} />
            ) : (
              <CredentialAssistant
                integration={integration}
                owner={owner}
                editCredentialId={editCredentialId}
                onDone={() => onOpenChange(false)}
              />
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
