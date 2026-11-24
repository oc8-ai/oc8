import { useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { ConnectDialog } from "@/components/connections/connect-dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { type Connection, useMyConnections } from "@/lib/connections";
import {
  type HireConnectionBody,
  type PersonalTemplateDTO,
  type TemplateConnectionDTO,
  useHirePersonalTemplate,
} from "@/lib/hooks";
import { useT } from "@/lib/i18n";
import { errorCode, hireErrorText, usableLogins } from "@/lib/personal-hire";

function toBody(integration: string, login: Connection): HireConnectionBody {
  return login.kind === "oauth"
    ? { integration, oauthConnectionId: login.id }
    : { integration, credentialId: login.id };
}

function LoginChoice({
  conn,
  logins,
  chosen,
  groupName,
  onChoose,
  onConnect,
}: {
  conn: TemplateConnectionDTO;
  logins: Connection[];
  chosen: string | undefined;
  groupName: string;
  onChoose: (id: string) => void;
  onConnect: () => void;
}) {
  const t = useT();
  return (
    <div className="mt-2 space-y-1 pl-6">
      {logins.length === 0 && (
        <p className="text-xs text-muted-foreground">
          {t(
            `You have no ${conn.label} login of your own yet.`,
            `Du hast noch keinen eigenen ${conn.label}-Zugang.`,
          )}
        </p>
      )}
      {logins.map((c) => (
        <label key={c.id} className="flex min-h-9 items-center gap-2 text-sm">
          <input
            type="radio"
            name={groupName}
            checked={chosen === c.id}
            onChange={() => onChoose(c.id)}
          />
          {c.accountLabel || conn.label}
        </label>
      ))}
      <button
        type="button"
        onClick={onConnect}
        className="text-xs font-medium text-primary hover:underline"
      >
        {t("Connect a new one", "Neuen verbinden")}
      </button>
    </div>
  );
}

/** Hiring a personal template that connects to a login: the member picks one
 *  of their own logins (or connects a new one), sees what the assistant
 *  gets, and consents to it using their account. */
export function PersonalHireDialog({
  template,
  open,
  onOpenChange,
}: {
  template: PersonalTemplateDTO;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useT();
  const qc = useQueryClient();
  const hire = useHirePersonalTemplate();
  const mineQuery = useMyConnections();
  const mine = useMemo(
    () => (Array.isArray(mineQuery.data) ? mineQuery.data : []),
    [mineQuery.data],
  );

  const alternatives = template.connectionsAny;
  const offered = alternatives.filter((a) => a.available);
  const [pickedAlt, setPickedAlt] = useState<string | null>(null);
  // One available alternative needs no choice.
  const alt = pickedAlt ?? (offered.length === 1 ? offered[0].integration : null);
  const [loginFor, setLoginFor] = useState<Record<string, string>>({});
  const [optIn, setOptIn] = useState<Record<string, boolean>>({});
  const [consent, setConsent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [connecting, setConnecting] = useState<string | null>(null);

  // What the member agreed to is the offer they saw: a new offer needs a new yes.
  // biome-ignore lint/correctness/useExhaustiveDependencies: reset on the token only
  useEffect(() => setConsent(false), [template.hireToken]);

  const chosenLogin = (integration: string) =>
    usableLogins(mine, integration).find((c) => c.id === loginFor[integration]);

  const optional = template.connectionsOptional.filter((o) => o.available && optIn[o.integration]);
  const hasLogins = alternatives.length > 0 || optional.length > 0;
  const altReady = alternatives.length === 0 || (alt !== null && !!chosenLogin(alt));
  const optReady = optional.every((o) => !!chosenLogin(o.integration));
  const canHire = altReady && optReady && consent && !hire.isPending;

  const submit = () => {
    const body: Parameters<typeof hire.mutate>[0] = {
      capaId: template.capaId,
      hireToken: template.hireToken,
    };
    if (hasLogins) {
      if (alt !== null) {
        const login = chosenLogin(alt);
        if (login) body.connection = toBody(alt, login);
      }
      body.optionalConnections = optional.flatMap((o) => {
        const login = chosenLogin(o.integration);
        return login ? [toBody(o.integration, login)] : [];
      });
      body.confirmShare = true;
    }
    setError(null);
    hire.mutate(body, {
      onSuccess: () => {
        toast.success(t("Assistant hired", "Assistent eingestellt"));
        onOpenChange(false);
      },
      onError: (err) => {
        setError(hireErrorText(err, t));
        // The offer changed: show the fresh one and keep this dialog open.
        if (errorCode(err) === "hire_changed")
          void qc.invalidateQueries({ queryKey: ["personal-department", "templates"] });
      },
    });
  };

  const connectAs = (name: string) => (
    <ConnectDialog
      integration={name}
      open={connecting === name}
      onOpenChange={(o) => setConnecting(o ? name : null)}
      defaultOwner="me"
    />
  );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{t(`Hire ${template.label}`, `${template.label} einstellen`)}</DialogTitle>
          {template.summary && <DialogDescription>{template.summary}</DialogDescription>}
        </DialogHeader>
        <div className="space-y-4 text-sm">
          <section>
            <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {t("What this assistant gets", "Was dieser Assistent bekommt")}
            </h3>
            <p className="mt-1 text-xs text-muted-foreground">{t("Tools", "Werkzeuge")}</p>
            {template.grants.tools.length === 0 ? (
              <p className="text-xs">{t("None", "Keine")}</p>
            ) : (
              <ul className="list-disc pl-5">
                {template.grants.tools.map((k) => (
                  <li key={k}>{k}</li>
                ))}
              </ul>
            )}
            <p className="mt-2 text-xs text-muted-foreground">{t("Skills", "Fähigkeiten")}</p>
            {template.grants.skills.length === 0 ? (
              <p className="text-xs">{t("None", "Keine")}</p>
            ) : (
              <ul className="list-disc pl-5">
                {template.grants.skills.map((k) => (
                  <li key={k}>{k}</li>
                ))}
              </ul>
            )}
          </section>

          {alternatives.length > 0 && (
            <fieldset>
              <legend className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                {t("Which login should it use?", "Welchen Zugang soll er verwenden?")}
              </legend>
              {alternatives.map((a) => (
                <div key={a.integration}>
                  <label className="flex min-h-9 items-center gap-2">
                    <input
                      type="radio"
                      name="hire-alternative"
                      disabled={!a.available}
                      checked={alt === a.integration}
                      onChange={() => setPickedAlt(a.integration)}
                    />
                    {a.label}
                    {!a.available && (
                      <span className="text-xs text-muted-foreground">
                        {t("Not available", "Nicht verfügbar")}
                      </span>
                    )}
                  </label>
                  {a.available && alt === a.integration && (
                    <LoginChoice
                      conn={a}
                      logins={usableLogins(mine, a.integration)}
                      chosen={loginFor[a.integration]}
                      groupName={`hire-login-${a.integration}`}
                      onChoose={(id) => setLoginFor((p) => ({ ...p, [a.integration]: id }))}
                      onConnect={() => setConnecting(a.integration)}
                    />
                  )}
                </div>
              ))}
              {offered.length === 0 && (
                <p className="mt-1 text-xs text-muted-foreground">
                  {t(
                    "None of the logins this assistant needs is available in your organisation.",
                    "Keiner der Zugänge, die dieser Assistent braucht, ist in deiner Organisation verfügbar.",
                  )}
                </p>
              )}
            </fieldset>
          )}

          {template.connectionsOptional.length > 0 && (
            <fieldset>
              <legend className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                {t("Optional logins", "Optionale Zugänge")}
              </legend>
              {template.connectionsOptional.map((o) => (
                <div key={o.integration}>
                  <label className="flex min-h-9 items-center gap-2">
                    <input
                      type="checkbox"
                      disabled={!o.available}
                      checked={!!optIn[o.integration] && o.available}
                      onChange={(e) =>
                        setOptIn((p) => ({ ...p, [o.integration]: e.target.checked }))
                      }
                    />
                    {o.label}
                    {!o.available && (
                      <span className="text-xs text-muted-foreground">
                        {t("Not available", "Nicht verfügbar")}
                      </span>
                    )}
                  </label>
                  {o.available && optIn[o.integration] && (
                    <LoginChoice
                      conn={o}
                      logins={usableLogins(mine, o.integration)}
                      chosen={loginFor[o.integration]}
                      groupName={`hire-login-${o.integration}`}
                      onChoose={(id) => setLoginFor((p) => ({ ...p, [o.integration]: id }))}
                      onConnect={() => setConnecting(o.integration)}
                    />
                  )}
                </div>
              ))}
            </fieldset>
          )}

          <label className="flex items-start gap-2">
            <input
              type="checkbox"
              className="mt-1"
              checked={consent}
              onChange={(e) => setConsent(e.target.checked)}
            />
            {t(
              "Let this assistant use my account. It acts only for me.",
              "Diesen Assistenten mein Konto nutzen lassen. Er handelt nur für mich.",
            )}
          </label>

          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
        </div>
        <DialogFooter>
          <button
            type="button"
            onClick={() => onOpenChange(false)}
            className="inline-flex h-9 items-center rounded-md border border-border px-3 text-sm"
          >
            {t("Cancel", "Abbrechen")}
          </button>
          <button
            type="button"
            disabled={!canHire}
            onClick={submit}
            className="inline-flex h-9 items-center gap-1.5 rounded-md bg-primary px-3 text-sm font-medium text-primary-foreground disabled:opacity-50"
          >
            {hire.isPending && <Loader2 className="h-3 w-3 animate-spin" />}
            {t("Hire", "Einstellen")}
          </button>
        </DialogFooter>
        {alt !== null && connectAs(alt)}
        {template.connectionsOptional.map((o) =>
          o.integration !== alt ? <div key={o.integration}>{connectAs(o.integration)}</div> : null,
        )}
      </DialogContent>
    </Dialog>
  );
}
