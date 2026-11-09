import { useEffect, useRef, useState } from "react";
import { Link } from "@tanstack/react-router";
import { Plug } from "lucide-react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { useConfirm } from "@/hooks/use-confirm";
import {
  type Connection,
  type Integration,
  type Owner,
  OAuthCompleteError,
  connectionStateOf,
  useAdminConsentUrl,
  useCompleteOAuth,
  useDisconnect,
  useIntegrations,
  useMemberConnections,
  useMyConnections,
  useOrganisationConnections,
  useTestConnection,
} from "@/lib/connections";
import { useMay } from "@/lib/governance-hooks";
import { useLang, useT } from "@/lib/i18n";
import { ConnectDialog } from "./connect-dialog";
import { IntegrationIcon } from "./integration-icon";
import { OrganisationCredentials } from "./organisation-credentials";
import { ReturnError } from "./return-error";
import { checkErrorText, oneLineBenefit, providerName } from "./texts";

export interface ConnectionsSearch {
  /** The OAuth callback's one-time id, exchanged via `POST /oauth/{provider}/complete`. */
  pending?: string;
  error?: string;
  provider?: string;
  connect?: string;
}

interface DialogState {
  integration: string;
  owner: Owner;
  editCredentialId?: string;
}

/** What the OAuth return left to show, captured once (the URL is cleaned). */
type ReturnState =
  | { kind: "none" }
  | { kind: "completing" }
  | { kind: "connected"; accountLabel: string }
  | { kind: "error"; code: string; adminConsentUrl: string | null };

const SMALL =
  "inline-flex min-h-11 items-center rounded-md border border-border px-3 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-50";
const PRIMARY =
  "inline-flex min-h-11 items-center rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2";

function since(iso: string, lang: string): string {
  return new Date(iso).toLocaleDateString(lang === "de" ? "de-DE" : "en-GB", {
    day: "numeric",
    month: "short",
  });
}

function initialReturn(search: ConnectionsSearch): ReturnState {
  if (search.error) return { kind: "error", code: search.error, adminConsentUrl: null };
  if (search.pending) return { kind: "completing" };
  return { kind: "none" };
}

/** The page carries a one-time id in its first URL: while it is open, no
 *  request or link may pass the address on as a Referer. */
function useNoReferrer() {
  useEffect(() => {
    const meta = document.createElement("meta");
    meta.name = "referrer";
    meta.content = "no-referrer";
    document.head.prepend(meta);
    return () => meta.remove();
  }, []);
}

export function ConnectionsPage({
  search,
  onConsumeSearch,
}: {
  search: ConnectionsSearch;
  onConsumeSearch: () => void;
}) {
  const t = useT();
  const { lang } = useLang();
  const may = useMay();
  const isAdmin = may("integration:manage");
  const { confirm, ConfirmDialog } = useConfirm();
  useNoReferrer();
  // Captured once: the URL is cleaned right away, and a refetch must not
  // close what the return or a call to action opened.
  const [returned, setReturned] = useState<ReturnState>(() => initialReturn(search));
  const [dialog, setDialog] = useState<DialogState | null>(() =>
    search.connect ? { integration: search.connect, owner: "me" } : null,
  );
  const complete = useCompleteOAuth();
  // Latched: StrictMode's second effect run and every re-render must never
  // post the one-time id twice.
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    if (search.pending || search.error || search.connect) onConsumeSearch();
    // A reported error wins: the pending id is then never posted.
    if (!search.pending || search.error) return;
    const provider = search.provider;
    if (provider !== "microsoft" && provider !== "google") {
      setReturned({ kind: "error", code: "provider_error", adminConsentUrl: null });
      return;
    }
    complete.mutateAsync({ provider, pending: search.pending }).then(
      (connection) => setReturned({ kind: "connected", accountLabel: connection.accountLabel }),
      (err: unknown) =>
        setReturned(
          err instanceof OAuthCompleteError
            ? { kind: "error", code: err.code, adminConsentUrl: err.adminConsentUrl }
            : { kind: "error", code: "exchange_failed", adminConsentUrl: null },
        ),
    );
  }, [search, onConsumeSearch, complete]);

  // A call to action followed while the page is already open (same
  // instance): open its assistant once per request, then clean the URL. The
  // first one was taken over by the state initialiser above.
  const handledConnect = useRef(search.connect);
  useEffect(() => {
    if (!search.connect) {
      handledConnect.current = undefined;
      return;
    }
    if (handledConnect.current === search.connect) return;
    handledConnect.current = search.connect;
    setDialog({ integration: search.connect, owner: "me" });
    onConsumeSearch();
  }, [search.connect, onConsumeSearch]);

  // A `?error=admin_consent_required` from the callback carries no link:
  // fetch the one the IT administrator opens.
  const needsConsentUrl =
    returned.kind === "error" &&
    returned.code === "admin_consent_required" &&
    !returned.adminConsentUrl;
  const consentUrl = useAdminConsentUrl(needsConsentUrl);

  const integrations = useIntegrations();
  const mine = useMyConnections();
  const org = useOrganisationConnections(isAdmin);
  const members = useMemberConnections(isAdmin);
  const disconnect = useDisconnect();
  const test = useTestConnection();

  const available = (integrations.data ?? []).filter((i) => i.available);
  const byName = new Map((integrations.data ?? []).map((i) => [i.name, i] as const));
  const integrationTypes = (integrations.data ?? []).flatMap((i) =>
    i.credentialType ? [i.credentialType] : [],
  );
  const labelOf = (c: Connection) =>
    (c.integration ? byName.get(c.integration)?.label : undefined) ||
    providerName(c.provider) ||
    c.credentialType ||
    "";

  const revoke = async (c: Connection, who: string | null) => {
    const ok = await confirm({
      title: who
        ? t("Revoke this connection?", "Verbindung widerrufen?")
        : t("Disconnect?", "Verbindung trennen?"),
      description: who
        ? t(
            `${who} loses this connection (${c.accountLabel}). Access is revoked at the provider too.`,
            `${who} verliert diese Verbindung (${c.accountLabel}). Der Zugriff wird auch beim Anbieter widerrufen.`,
          )
        : t(
            `Agents can then no longer use ${c.accountLabel}.`,
            `Agenten können ${c.accountLabel} dann nicht mehr nutzen.`,
          ),
      confirmLabel: who ? t("Revoke", "Widerrufen") : t("Disconnect", "Trennen"),
      cancelLabel: t("Cancel", "Abbrechen"),
    });
    if (!ok) return;
    try {
      await disconnect.mutateAsync(c);
      toast.success(t("Done.", "Erledigt."));
    } catch {
      toast.error(
        t(
          "That did not work. Please try again.",
          "Das hat nicht geklappt. Bitte versuchen Sie es erneut.",
        ),
      );
    }
  };

  const runTest = async (c: Connection) => {
    try {
      const result = await test.mutateAsync(c.id);
      if (result.ok) toast.success(t("Connection works.", "Verbindung funktioniert."));
      else toast.error(checkErrorText(result, t));
    } catch {
      toast.error(
        t(
          "The test failed. Please try again.",
          "Der Test ist fehlgeschlagen. Bitte versuchen Sie es erneut.",
        ),
      );
    }
  };

  const open = (integration: string, owner: Owner, editCredentialId?: string) =>
    setDialog({ integration, owner, editCredentialId });

  return (
    <div className="space-y-4">
      {returned.kind === "completing" && (
        <Panel className="p-4 text-sm text-muted-foreground">
          <p role="status">{t("Finishing the connection …", "Verbindung wird abgeschlossen …")}</p>
        </Panel>
      )}
      {returned.kind === "connected" && (
        <Panel className="p-4 text-sm">
          <p role="status">
            {t(`Connected as ${returned.accountLabel}.`, `Verbunden als ${returned.accountLabel}.`)}
          </p>
        </Panel>
      )}
      {returned.kind === "error" && (
        <Panel className="p-4">
          <ReturnError
            code={returned.code}
            adminConsentUrl={returned.adminConsentUrl ?? consentUrl.data?.url ?? null}
          />
        </Panel>
      )}

      <Panel className="p-6">
        <section aria-labelledby="connections-mine">
          <div className="flex items-start gap-3">
            <div className="grid h-9 w-9 place-items-center rounded-md bg-primary/15 text-primary">
              <Plug className="size-4" />
            </div>
            <div>
              <h2 id="connections-mine" className="font-serif text-lg">
                {t("My connections", "Meine Verbindungen")}
              </h2>
              <p className="mt-1 text-sm text-muted-foreground">
                {t(
                  "Your own accounts, used when an agent works for you.",
                  "Ihre eigenen Konten, die ein Agent nutzt, wenn er für Sie arbeitet.",
                )}
              </p>
            </div>
          </div>
          {!integrations.isLoading && available.length === 0 ? (
            <p className="mt-6 text-sm text-muted-foreground">
              {isAdmin ? (
                <Link to="/capas" className="underline">
                  {t("Install an integration", "Integration installieren")}
                </Link>
              ) : (
                t(
                  "Your admin has not set up an integration yet.",
                  "Ihr Admin hat noch keine Integration eingerichtet.",
                )
              )}
            </p>
          ) : (
            <ul className="mt-6 grid gap-3 sm:grid-cols-2">
              {available.map((integration) => (
                <MyCard
                  key={integration.name}
                  integration={integration}
                  mine={mine.data ?? []}
                  onConnect={() => open(integration.name, "me")}
                  onDisconnect={(c) => revoke(c, null)}
                  lang={lang}
                />
              ))}
            </ul>
          )}
        </section>
      </Panel>

      {isAdmin && (
        <Panel className="p-6">
          <section aria-labelledby="connections-org">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <h2 id="connections-org" className="font-serif text-lg">
                {t("Organisation", "Organisation")}
              </h2>
              {available.length > 0 && (
                <select
                  aria-label={t("Add access", "Zugang hinzufügen")}
                  className="min-h-11 rounded-md border border-border bg-background px-3 text-sm"
                  value=""
                  onChange={(event) =>
                    event.target.value && open(event.target.value, "organisation")
                  }
                >
                  <option value="">{t("Add access …", "Zugang hinzufügen …")}</option>
                  {available.map((i) => (
                    <option key={i.name} value={i.name}>
                      {i.label}
                    </option>
                  ))}
                </select>
              )}
            </div>
            {(org.data ?? []).length === 0 ? (
              <p className="mt-4 text-sm text-muted-foreground">
                {t("No organisation access yet.", "Noch kein Zugang der Organisation.")}
              </p>
            ) : (
              <div className="mt-4 overflow-x-auto">
                <table className="w-full text-sm">
                  <tbody>
                    {(org.data ?? []).map((c) => (
                      <tr key={c.id} className="border-b border-border/50">
                        <td className="py-3 pr-3">{labelOf(c)}</td>
                        <td className="py-3 pr-3">{c.accountLabel}</td>
                        <td className="py-3 pr-3 text-muted-foreground">{c.createdByName ?? ""}</td>
                        <td className="py-3 pr-3 text-muted-foreground">
                          {since(c.createdAt, lang)}
                        </td>
                        <td className="py-3 pr-3">
                          <StatusBadge connection={c} />
                        </td>
                        <td className="py-3 text-right whitespace-nowrap">
                          {c.kind === "credential" && (
                            <>
                              <button type="button" className={SMALL} onClick={() => runTest(c)}>
                                {t("Test", "Testen")}
                              </button>{" "}
                              {c.integration && (
                                <>
                                  <button
                                    type="button"
                                    className={SMALL}
                                    onClick={() =>
                                      open(c.integration as string, "organisation", c.id)
                                    }
                                  >
                                    {t("Edit", "Bearbeiten")}
                                  </button>{" "}
                                </>
                              )}
                            </>
                          )}
                          {c.kind === "oauth" && c.status === "needs_reauth" && c.integration && (
                            <>
                              <button
                                type="button"
                                className={SMALL}
                                onClick={() => open(c.integration as string, "organisation")}
                              >
                                {t("Reconnect", "Erneut verbinden")}
                              </button>{" "}
                            </>
                          )}
                          <button type="button" className={SMALL} onClick={() => revoke(c, null)}>
                            {t("Remove", "Entfernen")}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <div className="mt-6">
              <OrganisationCredentials excludeTypes={integrationTypes} />
            </div>
          </section>
        </Panel>
      )}

      {isAdmin && (
        <Panel className="p-6">
          <section aria-labelledby="connections-members">
            <h2 id="connections-members" className="font-serif text-lg">
              {t("Members", "Mitglieder")}
            </h2>
            {(members.data ?? []).length === 0 ? (
              <p className="mt-4 text-sm text-muted-foreground">
                {t(
                  "No member has connected an account yet.",
                  "Noch kein Mitglied hat ein Konto verbunden.",
                )}
              </p>
            ) : (
              <div className="mt-4 overflow-x-auto">
                <table className="w-full text-sm">
                  <tbody>
                    {(members.data ?? []).map((c) => (
                      <tr key={c.id} className="border-b border-border/50">
                        <td className="py-3 pr-3 font-medium">{c.ownerName}</td>
                        <td className="py-3 pr-3">{labelOf(c)}</td>
                        <td className="py-3 pr-3">{c.accountLabel}</td>
                        <td className="py-3 pr-3 text-muted-foreground">
                          {since(c.createdAt, lang)}
                        </td>
                        <td className="py-3 text-right">
                          <button
                            type="button"
                            className={SMALL}
                            onClick={() =>
                              revoke(c, c.ownerName ?? t("This member", "Dieses Mitglied"))
                            }
                          >
                            {t("Revoke", "Widerrufen")}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </Panel>
      )}

      {dialog && (
        <ConnectDialog
          integration={dialog.integration}
          defaultOwner={dialog.owner}
          editCredentialId={dialog.editCredentialId}
          open
          onOpenChange={(next) => !next && setDialog(null)}
        />
      )}
      {ConfirmDialog}
    </div>
  );
}

function StatusBadge({ connection }: { connection: Connection }) {
  const t = useT();
  if (connection.status === "needs_reauth" || connection.status === "failed") {
    return (
      <span className="rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-xs">
        {connection.status === "failed"
          ? t("Test failed", "Test fehlgeschlagen")
          : t("Reconnect", "Erneut verbinden")}
      </span>
    );
  }
  if (connection.status === "untested") {
    return (
      <span className="text-xs text-muted-foreground">{t("Not tested", "Nicht getestet")}</span>
    );
  }
  return <span className="text-xs text-muted-foreground">{t("Active", "Aktiv")}</span>;
}

function MyCard({
  integration,
  mine,
  onConnect,
  onDisconnect,
  lang,
}: {
  integration: Integration;
  mine: Connection[];
  onConnect: () => void;
  onDisconnect: (c: Connection) => void;
  lang: string;
}) {
  const t = useT();
  const { state, connections } = connectionStateOf(integration, mine);
  const orange = state === "needs_reauth";
  return (
    <li
      className={
        orange
          ? "rounded-lg border border-amber-500/40 bg-amber-500/10 p-4"
          : "rounded-lg border border-border p-4"
      }
    >
      <div className="flex items-center gap-2">
        <IntegrationIcon integration={integration} size={24} />
        <p className="font-medium">{integration.label}</p>
      </div>
      {state === "not_connected" && (
        <>
          <p className="mt-1 text-sm text-muted-foreground">{oneLineBenefit(integration, t)}</p>
          <button
            type="button"
            className={`${PRIMARY} mt-3`}
            aria-label={t(`Connect ${integration.label}`, `${integration.label} verbinden`)}
            onClick={onConnect}
          >
            {t("Connect", "Verbinden")}
          </button>
        </>
      )}
      {state !== "not_connected" &&
        connections.map((c) => (
          <div key={c.id} className="mt-2 flex items-center justify-between gap-2 text-sm">
            <span className="min-w-0 break-words">
              {t(
                `Connected as ${c.accountLabel} · since ${since(c.createdAt, lang)}`,
                `Verbunden als ${c.accountLabel} · seit ${since(c.createdAt, lang)}`,
              )}
            </span>
            <button type="button" className={SMALL} onClick={() => onDisconnect(c)}>
              {t("Disconnect", "Trennen")}
            </button>
          </div>
        ))}
      {orange && (
        <button type="button" className={`${PRIMARY} mt-3`} onClick={onConnect}>
          {t("Reconnect", "Erneut verbinden")}
        </button>
      )}
    </li>
  );
}
