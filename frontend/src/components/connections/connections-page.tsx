import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "@tanstack/react-router";
import { Plus, PlugZap } from "lucide-react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { CustomMcpWizard } from "@/components/custom-mcp-wizard";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
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
import { useEnablePlugin } from "@/lib/hooks";
import { useLang, useT } from "@/lib/i18n";
import { ConnectDialog } from "./connect-dialog";
import { ConnectionRow, RowList, RowMenu } from "./connection-row";
import { IntegrationIcon } from "./integration-icon";
import { MemberConnections } from "./member-connections";
import { OrganisationCredentials } from "./organisation-credentials";
import { ReturnError } from "./return-error";
import { checkErrorText, isBroken, providerName, shortDate } from "./texts";

export const CONNECTIONS_TABS = ["mine", "organisation", "members"] as const;
export type ConnectionsTab = (typeof CONNECTIONS_TABS)[number];

export interface ConnectionsSearch {
  /** The OAuth callback's one-time id, exchanged via `POST /oauth/{provider}/complete`. */
  pending?: string;
  error?: string;
  provider?: string;
  connect?: string;
  /** The administrator's open tab; "mine" when absent. */
  tab?: ConnectionsTab;
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
  onTabChange,
}: {
  search: ConnectionsSearch;
  onConsumeSearch: () => void;
  /** Puts the chosen tab into the URL, so a link opens the same tab. */
  onTabChange: (tab: ConnectionsTab) => void;
}) {
  const t = useT();
  const { lang } = useLang();
  const may = useMay();
  const isAdmin = may("integration:manage");
  // Making an installed plugin available enables it: the backend's enable
  // needs plugin:manage, so only offer it to whoever holds that.
  const mayEnable = may("plugin:manage");
  const { confirm, ConfirmDialog } = useConfirm();
  const [customOpen, setCustomOpen] = useState(false);
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
  const enable = useEnablePlugin();

  const available = (integrations.data ?? []).filter((i) => i.available);
  const notYetAvailable = (integrations.data ?? []).filter((i) => !i.available);
  const byName = new Map((integrations.data ?? []).map((i) => [i.name, i] as const));
  const integrationTypes = (integrations.data ?? []).flatMap((i) =>
    i.credentialType ? [i.credentialType] : [],
  );
  const integrationList = integrations.data;
  const labelOf = useCallback(
    (c: Connection) =>
      (c.integration ? integrationList?.find((i) => i.name === c.integration)?.label : undefined) ||
      providerName(c.provider) ||
      c.credentialType ||
      "",
    [integrationList],
  );
  const orgRows = org.data ?? [];
  const memberRows = members.data ?? [];
  const tab: ConnectionsTab = isAdmin ? (search.tab ?? "mine") : "mine";

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
          "Das hat nicht geklappt. Bitte versuch es erneut.",
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
          "Der Test ist fehlgeschlagen. Bitte versuch es erneut.",
        ),
      );
    }
  };

  const makeAvailable = async (i: Integration) => {
    try {
      await enable.mutateAsync({ pluginId: i.capaId, grantedPermissions: i.permissions ?? [] });
      toast.success(t(`${i.label} is available.`, `${i.label} ist verfügbar.`));
    } catch (err: unknown) {
      toast.error(
        err instanceof Error
          ? err.message
          : t(
              "That did not work. Please try again.",
              "Das hat nicht geklappt. Bitte versuch es erneut.",
            ),
      );
    }
  };

  const open = (integration: string, owner: Owner, editCredentialId?: string) =>
    setDialog({ integration, owner, editCredentialId });

  const mineList = (
    <MineList
      available={available}
      notYetAvailable={isAdmin && mayEnable ? notYetAvailable : []}
      mine={mine.data ?? []}
      organisation={orgRows}
      loading={integrations.isLoading}
      mayInstall={mayEnable}
      enabling={enable.isPending}
      lang={lang}
      onConnect={(name) => open(name, "me")}
      onDisconnect={(c) => revoke(c, null)}
      onMakeAvailable={makeAvailable}
      onAddCustom={() => setCustomOpen(true)}
    />
  );

  return (
    <div className="space-y-4">
      {returned.kind === "completing" && (
        <p
          role="status"
          className="rounded-md border border-border px-4 py-3 text-sm text-muted-foreground"
        >
          {t("Finishing the connection …", "Verbindung wird abgeschlossen …")}
        </p>
      )}
      {returned.kind === "connected" && (
        <p role="status" className="rounded-md border border-border px-4 py-3 text-sm">
          {t(`Connected as ${returned.accountLabel}.`, `Verbunden als ${returned.accountLabel}.`)}
        </p>
      )}
      {returned.kind === "error" && (
        <div className="rounded-md border border-border px-4 py-3">
          <ReturnError
            code={returned.code}
            adminConsentUrl={returned.adminConsentUrl ?? consentUrl.data?.url ?? null}
          />
        </div>
      )}

      {isAdmin ? (
        <Tabs value={tab} onValueChange={(next) => onTabChange(next as ConnectionsTab)}>
          <TabsList aria-label={t("Connections", "Verbindungen")}>
            <TabsTrigger value="mine">{t("Mine", "Meine")}</TabsTrigger>
            <TabsTrigger value="organisation">{t("Organisation", "Organisation")}</TabsTrigger>
            <TabsTrigger value="members">
              {t(`Members (${memberRows.length})`, `Mitglieder (${memberRows.length})`)}
            </TabsTrigger>
          </TabsList>
          <TabsContent value="mine" className="mt-3">
            <Panel className="px-4 py-1">{mineList}</Panel>
          </TabsContent>
          <TabsContent value="organisation" className="mt-3">
            <Panel className="px-4 py-3">
              <OrganisationAccesses
                rows={orgRows}
                available={available}
                byName={byName}
                labelOf={labelOf}
                lang={lang}
                onAdd={(name) => open(name, "organisation")}
                onEdit={(c) => open(c.integration as string, "organisation", c.id)}
                onReconnect={(c) => open(c.integration as string, "organisation")}
                onTest={runTest}
                onRemove={(c) => revoke(c, null)}
              />
              <OrganisationCredentials excludeTypes={integrationTypes} />
              <MoreIntegrationsHint mayInstall={mayEnable} />
            </Panel>
          </TabsContent>
          <TabsContent value="members" className="mt-3">
            <Panel className="px-4 py-3">
              <MemberConnections
                rows={memberRows}
                labelOf={labelOf}
                lang={lang}
                onRevoke={(c) => revoke(c, c.ownerName ?? t("This member", "Dieses Mitglied"))}
              />
            </Panel>
          </TabsContent>
        </Tabs>
      ) : (
        <section aria-label={t("My connections", "Meine Verbindungen")}>
          <Panel className="px-4 py-1">{mineList}</Panel>
        </section>
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
      {customOpen && <CustomMcpWizard open={customOpen} onOpenChange={setCustomOpen} />}
      {ConfirmDialog}
    </div>
  );
}

const WARN = "text-amber-600 dark:text-amber-400";

/** Covered for everyone: a working organisation access to a login-based app. */
function coveredByOrganisation(integration: Integration, organisation: readonly Connection[]) {
  return (
    integration.kind === "credential" &&
    organisation.some((c) => c.integration === integration.name && !isBroken(c))
  );
}

function MineList({
  available,
  notYetAvailable,
  mine,
  organisation,
  loading,
  mayInstall,
  enabling,
  lang,
  onConnect,
  onDisconnect,
  onMakeAvailable,
  onAddCustom,
}: {
  available: Integration[];
  notYetAvailable: Integration[];
  mine: Connection[];
  organisation: Connection[];
  loading: boolean;
  /** plugin:manage: may install another integration under Capas. */
  mayInstall: boolean;
  enabling: boolean;
  lang: string;
  onConnect: (integration: string) => void;
  onDisconnect: (c: Connection) => void;
  onMakeAvailable: (i: Integration) => void;
  onAddCustom: () => void;
}) {
  const t = useT();
  const nothing = !loading && available.length === 0;
  return (
    <>
      {nothing && <MoreIntegrationsHint mayInstall={mayInstall} />}
      {(available.length > 0 || notYetAvailable.length > 0 || mayInstall) && (
        <RowList>
          {available.flatMap((integration) => {
            const icon = <IntegrationIcon integration={integration} size={20} />;
            const { connections } = connectionStateOf(integration, mine);
            if (connections.length === 0) {
              const covered = coveredByOrganisation(integration, organisation);
              return [
                <ConnectionRow
                  key={integration.name}
                  icon={icon}
                  title={integration.label}
                  detail={
                    covered
                      ? t("Organisation access active", "Organisationszugang aktiv")
                      : t("Not connected", "Nicht verbunden")
                  }
                >
                  <Button
                    size="sm"
                    variant={covered ? "outline" : "default"}
                    aria-label={t(`Connect ${integration.label}`, `${integration.label} verbinden`)}
                    onClick={() => onConnect(integration.name)}
                  >
                    {t("Connect", "Verbinden")}
                  </Button>
                </ConnectionRow>,
              ];
            }
            return connections.map((c) => {
              const broken = isBroken(c);
              return (
                <ConnectionRow
                  key={c.id}
                  icon={icon}
                  title={integration.label}
                  warn={broken}
                  detail={
                    broken
                      ? `${c.accountLabel} · ${t("Needs reconnecting", "Erneut verbinden nötig")}`
                      : t(
                          `${c.accountLabel} · since ${shortDate(c.createdAt, lang)}`,
                          `${c.accountLabel} · seit ${shortDate(c.createdAt, lang)}`,
                        )
                  }
                >
                  {broken && (
                    <Button size="sm" onClick={() => onConnect(integration.name)}>
                      {t("Reconnect", "Erneut verbinden")}
                    </Button>
                  )}
                  <Button
                    size="sm"
                    variant={broken ? "ghost" : "outline"}
                    aria-label={t(`Disconnect ${c.accountLabel}`, `${c.accountLabel} trennen`)}
                    onClick={() => onDisconnect(c)}
                  >
                    {t("Disconnect", "Trennen")}
                  </Button>
                </ConnectionRow>
              );
            });
          })}
          {notYetAvailable.map((integration) => (
            <ConnectionRow
              key={integration.name}
              icon={<IntegrationIcon integration={integration} size={20} />}
              title={integration.label}
              detail={t("Installed, not available yet", "Installiert, noch nicht verfügbar")}
            >
              <Button
                size="sm"
                variant="outline"
                disabled={enabling}
                aria-label={t(
                  `Make ${integration.label} available`,
                  `${integration.label} bereitstellen`,
                )}
                onClick={() => onMakeAvailable(integration)}
              >
                {t("Make available", "Bereitstellen")}
              </Button>
            </ConnectionRow>
          ))}
          {mayInstall && (
            <ConnectionRow
              icon={<PlugZap className="size-5 text-muted-foreground" aria-hidden />}
              title={t("Custom server or API", "Eigener Server oder eigene API")}
              detail={t(
                "Connect your own MCP server or a REST API.",
                "Verbinde deinen eigenen MCP-Server oder eine REST-API.",
              )}
            >
              <Button
                size="sm"
                variant="outline"
                aria-label={t(
                  "Add custom server or API",
                  "Eigenen Server oder eigene API hinzufügen",
                )}
                onClick={onAddCustom}
              >
                {t("Add", "Hinzufügen")}
              </Button>
            </ConnectionRow>
          )}
        </RowList>
      )}
      {!loading && !nothing && <MoreIntegrationsHint mayInstall={mayInstall} />}
    </>
  );
}

/** One quiet line below a list of integrations (and the empty state when
 *  none is installed): where another app comes from. A link to Capas for
 *  whoever may install plugins; for everyone else, whom to ask. */
function MoreIntegrationsHint({ mayInstall }: { mayInstall: boolean }) {
  const t = useT();
  return (
    <p className="py-3 text-xs text-muted-foreground">
      {t("Don't see your app?", "Deine App ist nicht dabei?")}{" "}
      {mayInstall ? (
        <Link to="/capas" className="underline underline-offset-2 hover:text-foreground">
          {t(
            "Find more integrations under Capas →",
            "Weitere Integrationen findest du unter Capas →",
          )}
        </Link>
      ) : (
        t(
          "Ask your admin to install it under Capas.",
          "Bitte deinen Admin, sie unter Capas zu installieren.",
        )
      )}
    </p>
  );
}

function statusText(c: Connection, t: (en: string, de: string) => string): string {
  if (c.status === "failed") return t("Test failed", "Test fehlgeschlagen");
  if (isBroken(c)) return t("Needs reconnecting", "Erneut verbinden nötig");
  if (c.status === "untested") return t("Not tested", "Nicht getestet");
  return t("Active", "Aktiv");
}

function OrganisationAccesses({
  rows,
  available,
  byName,
  labelOf,
  lang,
  onAdd,
  onEdit,
  onReconnect,
  onTest,
  onRemove,
}: {
  rows: Connection[];
  available: Integration[];
  byName: Map<string, Integration>;
  labelOf: (c: Connection) => string;
  lang: string;
  onAdd: (integration: string) => void;
  onEdit: (c: Connection) => void;
  onReconnect: (c: Connection) => void;
  onTest: (c: Connection) => void;
  onRemove: (c: Connection) => void;
}) {
  const t = useT();
  return (
    <section aria-labelledby="connections-org-accesses">
      <div className="flex items-center justify-between gap-3">
        <h3 id="connections-org-accesses" className="text-sm font-medium">
          {t("Organisation accesses", "Zugänge der Organisation")}
        </h3>
        {available.length > 0 && (
          <DropdownMenu modal={false}>
            <DropdownMenuTrigger asChild>
              <Button size="sm">
                <Plus />
                {t("Add access", "Zugang hinzufügen")}
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {available.map((i) => (
                <DropdownMenuItem key={i.name} onSelect={() => onAdd(i.name)}>
                  {i.label}
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>
      {rows.length === 0 ? (
        <p className="py-3 text-sm text-muted-foreground">
          {t("No organisation access yet.", "Noch kein Zugang der Organisation.")}
        </p>
      ) : (
        <RowList>
          {rows.map((c) => {
            const label = labelOf(c);
            const integration = c.integration ? byName.get(c.integration) : undefined;
            const items = [
              ...(c.kind === "credential"
                ? [{ label: t("Test", "Testen"), onSelect: () => onTest(c) }]
                : []),
              ...(c.kind === "credential" && c.integration
                ? [{ label: t("Edit", "Bearbeiten"), onSelect: () => onEdit(c) }]
                : []),
              ...(c.kind === "oauth" && c.status === "needs_reauth" && c.integration
                ? [{ label: t("Reconnect", "Erneut verbinden"), onSelect: () => onReconnect(c) }]
                : []),
              { label: t("Remove", "Entfernen"), destructive: true, onSelect: () => onRemove(c) },
            ];
            return (
              <ConnectionRow
                key={c.id}
                icon={integration ? <IntegrationIcon integration={integration} size={20} /> : null}
                title={label}
                detail={[c.accountLabel, c.createdByName, shortDate(c.createdAt, lang)]
                  .filter(Boolean)
                  .join(" · ")}
                aside={<span className={isBroken(c) ? WARN : undefined}>{statusText(c, t)}</span>}
              >
                <RowMenu
                  label={t(
                    `Actions for ${label} (${c.accountLabel})`,
                    `Aktionen für ${label} (${c.accountLabel})`,
                  )}
                  items={items}
                />
              </ConnectionRow>
            );
          })}
        </RowList>
      )}
    </section>
  );
}
