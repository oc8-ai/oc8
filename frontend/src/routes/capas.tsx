import { createFileRoute } from "@tanstack/react-router";
import {
  AlertTriangle,
  Archive,
  BadgeCheck,
  Bot,
  Box,
  Building2,
  CheckCircle2,
  Cog,
  Cpu,
  Download,
  GitBranch,
  Info,
  Layers,
  PlugZap,
  Power,
  Settings,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  UserRound,
  X,
} from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { CapaExportWizard } from "@/components/capa-export-wizard";
import { CustomMcpWizard } from "@/components/custom-mcp-wizard";
import { DetailSheet } from "@/components/detail-sheet";
import { ListToolbar, groupItems, type ListQueryState } from "@/components/list-toolbar";
import {
  healthStatusOf,
  McpConnectionBody,
  McpConnectionsPanel,
} from "@/components/mcp-connections";
import { CapaSetupDialog } from "@/components/capa-setup-dialog";
import { useCan } from "@/lib/governance-hooks";
import { resolveTranslation, useLang, useT } from "@/lib/i18n";
import {
  type DiscoveredCapa,
  type McpConnection,
  useAvailablePlugins,
  useCapaIcon,
  useDisablePlugin,
  useEnablePlugin,
  useInstallPluginFromDisk,
  useMcpConnections,
} from "@/lib/hooks";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/capas")({
  component: CapasPage,
});

// Every installable unit oc8 knows about is "a Capa" of one of these types
// (backend/src/oc8/plugins/manifest.py's PluginType) -- this page is a UI-only
// rename (Plugin -> Capa in labels/nav/copy). The API paths now live under
// /capas/* (renamed from /plugins/* in a prior task); the internal type
// strings and manifest field names are still unchanged on purpose, to avoid
// a mechanical rename across ~20 plugin directories for a label change.
const TYPE_LABELS: Record<string, { en: string; de: string }> = {
  skill: { en: "Skill", de: "Skill" },
  tool_pack: { en: "Tool pack", de: "Tool-Paket" },
  connector: { en: "Connector", de: "Connector" },
  department_template: { en: "Department template", de: "Abteilungsvorlage" },
  runtime_adapter: { en: "Runtime", de: "Runtime" },
  model_adapter: { en: "Model adapter", de: "Modell-Adapter" },
  approval_channel: { en: "Approval channel", de: "Freigabekanal" },
  agent_template: { en: "Agent template", de: "Agentenvorlage" },
  flow_template: { en: "Flow template", de: "Flow-Vorlage" },
  core_extension: { en: "Core extension", de: "Kern-Erweiterung" },
};

// Generic per-type mark, shown whenever a Capa declares no icon of its own
// (or its icon fails to load) -- reuses icons already established elsewhere
// in this app's nav rather than inventing a new visual vocabulary.
const TYPE_ICONS: Record<string, typeof Layers> = {
  skill: Sparkles,
  tool_pack: Box,
  connector: Box,
  department_template: Building2,
  runtime_adapter: Cog,
  model_adapter: Cpu,
  approval_channel: ShieldCheck,
  agent_template: Bot,
  flow_template: GitBranch,
  core_extension: Layers,
};

// Deterministic per-type tint for the card icon tile and its type tag --
// spread across the wheel by hand (not derived from anything) so the browse
// grid reads as visually distinct categories at a glance, the way the old
// mock's colored app icons did, without inventing per-Capa branding data
// that doesn't exist.
const TYPE_HUES: Record<string, number> = {
  skill: 145,
  tool_pack: 200,
  connector: 230,
  department_template: 280,
  runtime_adapter: 25,
  model_adapter: 310,
  approval_channel: 170,
  agent_template: 60,
  flow_template: 345,
  core_extension: 250,
};
const DEFAULT_TYPE_HUE = 220;

// A Capa's real trust levels (backend/src/oc8/capas/manifest.py's
// TrustLevel) -- the honest equivalent of the old mock's Free/Paid/Verified
// row. No ratings or download counts exist for Capas, so those don't get a
// fabricated stand-in here.
const TRUST_LABELS: Record<string, { en: string; de: string }> = {
  first_party: { en: "First-party", de: "First-party" },
  verified: { en: "Verified", de: "Verifiziert" },
  community: { en: "Community", de: "Community" },
};

const SURFACE_ROUTES: Record<string, string> = {
  knowledge: "/knowledge",
  models: "/models",
  agents: "/agents",
  skills: "/skills",
  flows: "/flows",
  integrations: "/capas",
  departments: "/departments",
  automation: "/routes",
};

// The manifest's declared surface name ("integrations") stays the internal
// value -- only how it reads here changes, matching the rest of this page's
// UI-only rename.
const SURFACE_LABELS: Record<string, string> = {
  integrations: "capas",
};

// `install_from_disk` (backend) / `useInstallPluginFromDisk` (frontend) NEVER
// auto-enables a capa -- every install lands in installationStatus
// "installed" and stays there until a separate, deliberate
// `useEnablePlugin()` call. So "installed" (never yet enabled) is a normal,
// expected resting state, not an archived one -- only "disabled" (was
// enabled, then turned off) and "quarantined" (something actively went
// wrong, per the ck_capa_installation_status CHECK constraint in
// backend/src/oc8/models/capas.py) are the "archived" analog here.
//
// One exception within "disabled": `install_from_disk`'s own Update flow
// disables the OLD version pending re-consent for the new one -- a normal,
// expected, temporary state right after clicking Update, not something the
// user asked to hide. Treating it as archived made an updated Capa
// disappear from the list entirely instead of showing the "Enable" prompt
// it actually needs (live user report, 2026-08-24).
const UPDATE_PENDING_CONSENT = "plugin update pending consent";
function isArchivedInstall(
  status: string | null | undefined,
  disabledReason?: string | null,
): boolean {
  if (status === "quarantined") return true;
  return status === "disabled" && disabledReason !== UPDATE_PENDING_CONSENT;
}

function typeLabelOf(t: ReturnType<typeof useT>, type: string): string {
  const label = TYPE_LABELS[type];
  return label ? t(label.en, label.de) : type || "—";
}

export function CapasPage() {
  const t = useT();
  const can = useCan();
  const mayManage = can("plugin:manage");
  const [tab, setTab] = useState<"installed" | "available">("installed");

  // <ListToolbar>-driven search/filter/group/pagination (Task 14), fed
  // straight into `useAvailablePlugins(params)` (Task 15). `includeArchived`
  // is repurposed below as the "Show disabled" toggle: `/capas/available`
  // scans disk per tenant, not a `SoftDeleteMixin` table, so there is no
  // server-side archive concept to send it as -- it only ever drives a
  // CLIENT-SIDE filter, scoped to the Installed tab (see `shown` below).
  // 200, not 20: the Installed/Available tabs split whatever ONE server
  // page returns (see the comment below), so a partial page produces an
  // arbitrary, often-odd count on each tab -- live-observed as 9 Installed
  // cards in a 2-column grid (5 left, 4 right), reading as if only 9 Capas
  // existed even though a "Next" page held more. A tenant's real Capa
  // catalog is small (tens, not thousands) and the backend itself caps
  // this at 200 (`Query(ge=1, le=200)`, api/v1/capas.py), so fetching that
  // many in one page -- the same "fetch generously, page client-side"
  // choice already used for department/agent lookups elsewhere -- makes
  // the tab split operate on the WHOLE catalog instead of an arbitrary
  // slice of it.
  const [queryState, setQueryState] = useState<ListQueryState>({
    search: "",
    filters: {},
    groupBy: null,
    includeArchived: false,
    page: 1,
    pageSize: 200,
  });
  const {
    data: pluginsPage,
    isLoading,
    error,
  } = useAvailablePlugins({
    search: queryState.search,
    filters: queryState.filters,
    groupBy: queryState.groupBy,
    page: queryState.page,
    pageSize: queryState.pageSize,
  });
  const plugins = pluginsPage?.items ?? [];
  const { data: connections = [] } = useMcpConnections();
  const install = useInstallPluginFromDisk();
  const enable = useEnablePlugin();
  const disable = useDisablePlugin();
  const [pending, setPending] = useState<string | null>(null);
  const [setupPlugin, setSetupPlugin] = useState<DiscoveredCapa | null>(null);
  const [detailPlugin, setDetailPlugin] = useState<DiscoveredCapa | null>(null);
  const [exportOpen, setExportOpen] = useState(false);
  const [customMcpOpen, setCustomMcpOpen] = useState(false);
  // Set from CustomMcpWizard's onExported hand-off, so the export wizard
  // that opens right after can pre-check that one capa instead of landing
  // on an empty Selection step -- cleared whenever the export dialog closes
  // so a later, unrelated "Export as Capa" click starts from a clean slate.
  const [exportTargetCapaId, setExportTargetCapaId] = useState<string | null>(null);

  // The Installed/Available tabs are an ADDITIONAL filter layer on top of
  // the toolbar's search/type/group -- they slice whatever page the toolbar
  // just fetched, the same way grouping only ever applies to the current
  // page (see skills.tsx's Task 16 note). A capa that matches the current
  // search/type/group but landed on a different page won't show up under
  // its tab until that page is reached; that's the same tradeoff pagination
  // already makes for grouping.
  const installedAll = useMemo(() => plugins.filter((p) => p.installed), [plugins]);
  const availableAll = useMemo(() => plugins.filter((p) => !p.installed), [plugins]);
  // "disabled"/"quarantined" is the closest analog to "archived" here (no
  // deletedAt on capas) -- hidden by default within the Installed tab,
  // revealed by "Show disabled", exactly like an archived skill. A
  // freshly-installed-but-never-enabled capa ("installed") is NOT archived --
  // it shows inline with its normal Enable affordance, same as before Task 17.
  const installedVisible = useMemo(
    () => installedAll.filter((p) => !isArchivedInstall(p.installationStatus, p.disabledReason)),
    [installedAll],
  );
  const shown =
    tab === "installed"
      ? queryState.includeArchived
        ? installedAll
        : installedVisible
      : availableAll;

  // Trust is a client-side-only narrowing on top of `shown`, the same way
  // the Installed/Available tab split already is (see the comment on
  // `installedAll`/`availableAll` above) -- pageSize 200 means `shown`
  // already holds the whole tab, so there's no partial-page mismatch to
  // worry about.
  const [trustFilter, setTrustFilter] = useState<string | null>(null);
  const shownFiltered = useMemo(
    () => (trustFilter ? shown.filter((p) => p.trust === trustFilter) : shown),
    [shown, trustFilter],
  );

  const grouped = useMemo(
    () => groupItems(shownFiltered, queryState.groupBy, (p) => typeLabelOf(t, p.type)),
    [shownFiltered, queryState.groupBy, t],
  );

  // A connection that came from a Capa's own setup flow is stamped with the
  // plugin's name server-side (see McpConnection.pluginName) -- group by
  // that so each Capa's card can show its own connection's live status
  // instead of a second, disconnected list above the cards.
  const connectionsByPlugin = useMemo(() => {
    const map = new Map<string, McpConnection[]>();
    for (const c of connections) {
      if (!c.pluginName) continue;
      const list = map.get(c.pluginName) ?? [];
      list.push(c);
      map.set(c.pluginName, list);
    }
    return map;
  }, [connections]);
  // Hand-created connections (no plugin stamp) have nowhere else to live --
  // shown standalone, only when there are any, so the common case (every
  // connection came from a Capa) shows no redundant empty section.
  const unmatchedConnections = useMemo(
    () => connections.filter((c) => !c.pluginName),
    [connections],
  );

  const onInstall = async (p: DiscoveredCapa) => {
    setPending(p.pluginId);
    try {
      await install.mutateAsync(p.pluginId);
      toast.success(t(`Installed ${p.label || p.name}`, `${p.label || p.name} installiert`));
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : t("Installation failed.", "Installation fehlgeschlagen."),
      );
    } finally {
      setPending(null);
    }
  };

  const onEnable = async (p: DiscoveredCapa) => {
    if (!p.databaseId) return;
    setPending(p.pluginId);
    try {
      await enable.mutateAsync({ pluginId: p.databaseId, grantedPermissions: p.permissions });
      toast.success(t(`Enabled ${p.label || p.name}`, `${p.label || p.name} aktiviert`));
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : t("Activation failed.", "Aktivierung fehlgeschlagen."),
      );
    } finally {
      setPending(null);
    }
  };

  const onDisable = async (p: DiscoveredCapa) => {
    if (!p.databaseId) return;
    setPending(p.pluginId);
    try {
      await disable.mutateAsync({ pluginId: p.databaseId });
      toast.success(t(`Disabled ${p.label || p.name}`, `${p.label || p.name} deaktiviert`));
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : t("Could not disable.", "Deaktivieren fehlgeschlagen."),
      );
    } finally {
      setPending(null);
    }
  };

  const closeExportWizard = () => {
    setExportOpen(false);
    setExportTargetCapaId(null);
  };

  return (
    <div className="space-y-6">
      <Panel className="p-5">
        <header className="flex items-start justify-between gap-3">
          <div className="flex items-start gap-3">
            <div className="grid h-9 w-9 place-items-center rounded-md bg-primary/15 text-primary">
              <Layers className="h-4 w-4" />
            </div>
            <div>
              <div className="font-serif text-lg leading-tight">{t("Capas", "Capas")}</div>
              <p className="mt-0.5 max-w-2xl text-xs text-muted-foreground">
                {t(
                  "A Capa is anything installable in oc8 — an agent template, an MCP tool pack, a guardrail set, a department template, a runtime, or a bundle of several.",
                  "Eine Capa ist alles Installierbare in oc8 — eine Agentenvorlage, ein MCP-Tool-Paket, Guardrails, eine Abteilungsvorlage, eine Runtime oder ein Bündel davon.",
                )}
              </p>
            </div>
          </div>
          {mayManage && (
            <div className="flex shrink-0 items-center gap-2">
              <button
                type="button"
                onClick={() => setCustomMcpOpen(true)}
                className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background/30 px-3 py-1.5 text-sm text-foreground transition hover:border-primary/50"
              >
                <PlugZap className="h-3.5 w-3.5" />
                {t("Add custom MCP server", "Eigenen MCP-Server hinzufügen")}
              </button>
              <button
                type="button"
                onClick={() => setExportOpen(true)}
                className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background/30 px-3 py-1.5 text-sm text-foreground transition hover:border-primary/50"
              >
                <Archive className="h-3.5 w-3.5" />
                {t("Export as Capa", "Als Capa exportieren")}
              </button>
            </div>
          )}
        </header>
      </Panel>

      {/* Two sections, not one long scroll: Installed carries the Capas
          themselves, each showing its own connection's live status inline
          where it has one -- Available is a separate, focused browse list. */}
      <div className="flex gap-1 rounded-lg border border-border p-1">
        <button
          type="button"
          onClick={() => {
            setTab("installed");
            setQueryState((s) => ({ ...s, filters: {}, page: 1 }));
            setTrustFilter(null);
          }}
          className={cn(
            "rounded-md px-3 py-1.5 text-sm transition",
            tab === "installed"
              ? "bg-primary text-primary-foreground"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {t("Installed", "Installiert")} ({installedAll.length})
        </button>
        <button
          type="button"
          onClick={() => {
            setTab("available");
            setQueryState((s) => ({ ...s, filters: {}, page: 1, includeArchived: false }));
            setTrustFilter(null);
          }}
          className={cn(
            "rounded-md px-3 py-1.5 text-sm transition",
            tab === "available"
              ? "bg-primary text-primary-foreground"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {t("Available", "Verfügbar")} ({availableAll.length})
        </button>
      </div>

      {/* !mayManage never sees Capa cards at all (install/enable is
          tenant-wide, admin-only -- see the permission note below), so their
          Installed tab falls back to the old standalone connections list:
          the one thing about a Capa this role can still act on. */}
      {tab === "installed" && !mayManage && <McpConnectionsPanel />}

      {!mayManage ? (
        tab === "available" && (
          <Panel className="flex items-center gap-3 p-5 text-sm text-muted-foreground">
            <ShieldAlert className="h-4 w-4 shrink-0" />
            {t(
              "Ask an administrator to install or enable new Capas.",
              "Bitte einen Administrator, neue Capas zu installieren oder zu aktivieren.",
            )}
          </Panel>
        )
      ) : (
        <>
          {tab === "installed" && unmatchedConnections.length > 0 && (
            <div className="grid gap-3 md:grid-cols-2">
              {unmatchedConnections.map((c) => (
                <Panel key={c.id} className="p-4">
                  <div className="mb-2 text-sm font-medium">{c.name}</div>
                  <McpConnectionBody connection={c} />
                </Panel>
              ))}
            </div>
          )}

          {/* One browse container: search + the category/trust chip rows
              that replaced the old Type dropdown and its "sehr unscheinbar"
              gray group-by-type headers -- clicking a chip now does the
              category-navigation job those headers were a weak stand-in
              for, so grouping is gone rather than duplicated. */}
          <Panel className="space-y-3 p-4">
            <ListToolbar
              config={{
                searchPlaceholder: t("Search Capas…", "Capas durchsuchen…"),
                filters: [],
                groupBy: [],
                // The toggle only applies within the Installed tab (see
                // `shown` above) -- hidden entirely on Available, where a
                // capa's install status is never "disabled" in the first place.
                showArchivedToggle: tab === "installed",
                archivedToggleLabel: t("Show disabled", "Deaktivierte anzeigen"),
              }}
              state={queryState}
              onStateChange={setQueryState}
              // The current TAB's count after the trust narrowing, not the
              // server's combined installed+available total -- that total
              // drove page math against a number neither tab's grid ever
              // actually displayed.
              totalCount={shownFiltered.length}
            />

            <div className="flex flex-wrap gap-1.5">
              <CategoryChip
                active={!queryState.filters.type}
                label={t("All", "Alle")}
                onClick={() => setQueryState((s) => ({ ...s, filters: {}, page: 1 }))}
              />
              {Object.entries(TYPE_LABELS).map(([value, label]) => (
                <CategoryChip
                  key={value}
                  active={queryState.filters.type === value}
                  label={t(label.en, label.de)}
                  onClick={() =>
                    setQueryState((s) => ({ ...s, filters: { type: value }, page: 1 }))
                  }
                />
              ))}
            </div>

            <div className="flex flex-wrap gap-1.5">
              <CategoryChip
                active={!trustFilter}
                label={t("Any trust level", "Jede Vertrauensstufe")}
                onClick={() => setTrustFilter(null)}
                muted
              />
              {Object.entries(TRUST_LABELS).map(([value, label]) => (
                <CategoryChip
                  key={value}
                  active={trustFilter === value}
                  label={t(label.en, label.de)}
                  onClick={() => setTrustFilter(value)}
                  muted
                />
              ))}
            </div>
          </Panel>

          {isLoading && (
            <Panel className="p-5 text-sm text-muted-foreground">
              {t("Loading…", "Wird geladen …")}
            </Panel>
          )}

          {error && (
            <Panel className="p-5 text-sm text-[color:var(--status-error)]">
              {t("Could not read the plugin folder.", "Plugin-Ordner konnte nicht gelesen werden.")}
            </Panel>
          )}

          {!isLoading && !error && shownFiltered.length === 0 && (
            <Panel className="p-5 text-sm text-muted-foreground">
              {tab === "installed"
                ? t("No Capas installed yet.", "Noch keine Capas installiert.")
                : t(
                    "Nothing new on disk. Drop a folder containing a plugin.toml into the plugins directory, then reload.",
                    "Nichts Neues auf der Festplatte. Einen Ordner mit einer plugin.toml ins Plugin-Verzeichnis legen und neu laden.",
                  )}
            </Panel>
          )}

          {shownFiltered.length > 0 && (
            <div className="space-y-6">
              {grouped.map(({ group, items }) => (
                <div key={group ?? "all"} className="space-y-3">
                  {group ? (
                    <h3 className="text-xs uppercase text-muted-foreground">{group}</h3>
                  ) : null}
                  <div className="grid gap-3 md:grid-cols-2">
                    {items.map((p) => {
                      // Shown only within the Installed tab, only once "Show
                      // disabled" is checked, and only for the actually
                      // archived statuses ("disabled"/"quarantined") -- a
                      // never-yet-enabled "installed" capa always renders via
                      // the normal Install/Enable branches below, never this
                      // one. The archived capa's "Restore" action reuses
                      // useEnablePlugin().
                      const showAsDisabled =
                        tab === "installed" &&
                        queryState.includeArchived &&
                        isArchivedInstall(p.installationStatus, p.disabledReason);
                      return (
                        <CapaCard
                          key={p.pluginId}
                          plugin={p}
                          connections={connectionsByPlugin.get(p.name) ?? []}
                          busy={pending === p.pluginId}
                          showRestore={showAsDisabled}
                          onInstall={() => onInstall(p)}
                          onEnable={() => onEnable(p)}
                          onDisable={() => onDisable(p)}
                          onDetail={() => setDetailPlugin(p)}
                          onConfigure={() => setSetupPlugin(p)}
                        />
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      )}

      {detailPlugin && (
        <CapaDetailSheet
          plugin={detailPlugin}
          connections={connectionsByPlugin.get(detailPlugin.name) ?? []}
          onConfigure={() => {
            setSetupPlugin(detailPlugin);
            setDetailPlugin(null);
          }}
          onClose={() => setDetailPlugin(null)}
        />
      )}

      {setupPlugin && <CapaSetupDialog plugin={setupPlugin} onClose={() => setSetupPlugin(null)} />}

      {customMcpOpen && (
        <CustomMcpWizard
          open={customMcpOpen}
          onOpenChange={setCustomMcpOpen}
          onExported={(capaId) => {
            setCustomMcpOpen(false);
            // Hand off to the existing export flow for this one capa,
            // pre-checked -- see closeExportWizard for the matching cleanup.
            setExportTargetCapaId(capaId);
            setExportOpen(true);
          }}
        />
      )}

      {exportOpen && (
        <div
          className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4"
          onClick={closeExportWizard}
        >
          <div
            className="w-full max-w-2xl rounded-xl border border-border bg-panel p-5 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-4 flex items-center justify-between">
              <h2 className="font-serif text-lg">{t("Export as Capa", "Als Capa exportieren")}</h2>
              <button
                type="button"
                onClick={closeExportWizard}
                aria-label={t("Close", "Schließen")}
                className="rounded-md p-1 text-muted-foreground hover:text-foreground"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <CapaExportWizard
              onClose={closeExportWizard}
              initialSelection={
                exportTargetCapaId
                  ? {
                      kind: "tool_pack",
                      id: exportTargetCapaId,
                      name:
                        plugins.find((p) => p.databaseId === exportTargetCapaId)?.label ||
                        plugins.find((p) => p.databaseId === exportTargetCapaId)?.name ||
                        exportTargetCapaId,
                    }
                  : undefined
              }
            />
          </div>
        </div>
      )}
    </div>
  );
}

function CapaIcon({ plugin }: { plugin: DiscoveredCapa }) {
  const { data: iconUrl } = useCapaIcon(plugin.databaseId);
  const TypeIcon = TYPE_ICONS[plugin.type] ?? Layers;
  const hue = TYPE_HUES[plugin.type] ?? DEFAULT_TYPE_HUE;
  return (
    <div
      className="grid h-10 w-10 shrink-0 place-items-center overflow-hidden rounded-lg"
      style={{
        background: `color-mix(in oklab, oklch(0.72 0.14 ${hue}) 18%, transparent)`,
        color: `oklch(0.6 0.14 ${hue})`,
      }}
    >
      {iconUrl ? (
        <img
          src={iconUrl}
          alt=""
          className="h-full w-full object-contain p-1.5"
          draggable={false}
        />
      ) : (
        <TypeIcon className="h-4 w-4" />
      )}
    </div>
  );
}

function CategoryChip({
  label,
  active,
  onClick,
  muted = false,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
  // The trust row is a secondary facet -- same interaction, quieter at
  // rest, so it doesn't visually compete with the primary category row.
  muted?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-full border px-3 py-1 text-xs transition",
        active
          ? "border-primary bg-primary text-primary-foreground"
          : muted
            ? "border-border/60 bg-transparent text-muted-foreground hover:border-primary/40 hover:text-foreground"
            : "border-border bg-background/40 text-foreground hover:border-primary/40",
      )}
    >
      {label}
    </button>
  );
}

// Small color dot standing in for the full test-connection/discovered-tools
// body on the card -- the detail sheet is where that lives now.
function ConnectionDot({ connections }: { connections: McpConnection[] }) {
  const t = useT();
  if (connections.length === 0) return null;
  // Worst status wins when a Capa has more than one connection, same
  // priority a single glance should read: a broken connection is the thing
  // worth noticing first.
  const statuses = connections.map(healthStatusOf);
  const status = statuses.includes("error") ? "error" : statuses.includes("ok") ? "ok" : "untested";
  const meta = {
    ok: { color: "var(--status-running)", label: t("Connected", "Verbunden") },
    error: { color: "var(--status-error)", label: t("Connection error", "Verbindungsfehler") },
    untested: { color: "var(--status-paused)", label: t("Untested", "Ungetestet") },
  }[status];
  return (
    <span className="inline-flex items-center gap-1.5 text-xs" style={{ color: meta.color }}>
      <span
        className="h-1.5 w-1.5 rounded-full"
        style={{
          background: meta.color,
          boxShadow: status === "ok" ? `0 0 6px ${meta.color}` : "none",
        }}
      />
      {meta.label}
    </span>
  );
}

// Task 13: shared by CapaCard (the gear icon on the grid card, for direct
// access) and CapaDetailSheet (the footer button, reached via the card's ⓘ
// icon) -- previously computed inline, identically, in only the latter. A
// plugin qualifies once it is installed, currently enabled, and declares a
// setup contract at all.
function canConfigurePlugin(plugin: DiscoveredCapa): boolean {
  return Boolean(plugin.installed && plugin.installationStatus === "enabled" && plugin.setup);
}

function CapaCard({
  plugin,
  connections,
  busy,
  showRestore = false,
  onInstall,
  onEnable,
  onDisable,
  onDetail,
  onConfigure,
}: {
  plugin: DiscoveredCapa;
  connections: McpConnection[];
  busy: boolean;
  // True only within the Installed tab, only once "Show disabled" is
  // checked, for a capa whose installationStatus isn't "enabled" -- the
  // closest analog to an archived skill (Task 16). Swaps the usual
  // Enable/Update controls for a single "Restore" action that calls the
  // SAME useEnablePlugin() mutation `onEnable` already wraps -- there is no
  // separate restore endpoint for Capas.
  showRestore?: boolean;
  onInstall: () => void;
  onEnable: () => void;
  onDisable: () => void;
  onDetail: () => void;
  // Opens CapaSetupDialog directly -- no detour through the ⓘ icon and
  // CapaDetailSheet first (Task 13's discoverability fix).
  onConfigure: () => void;
}) {
  const t = useT();
  const { lang } = useLang();
  const typeLabel = TYPE_LABELS[plugin.type];
  const canConfigure = canConfigurePlugin(plugin);

  return (
    <Panel className={cn("flex flex-col gap-2.5 p-4", showRestore && "opacity-60")}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-2.5">
          <CapaIcon plugin={plugin} />
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="truncate font-medium">{plugin.label || plugin.name}</span>
              {plugin.trust === "first_party" && (
                <BadgeCheck className="h-3.5 w-3.5 shrink-0 text-primary" />
              )}
              {showRestore && (
                <span className="rounded-full border border-[color:var(--status-warning)]/40 bg-[color:var(--status-warning)]/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-[color:var(--status-warning)]">
                  {t("Disabled", "Deaktiviert")}
                </span>
              )}
              {!showRestore && plugin.disabledReason === UPDATE_PENDING_CONSENT && (
                <span
                  className="rounded-full border border-[color:var(--status-warning)]/40 bg-[color:var(--status-warning)]/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-[color:var(--status-warning)]"
                  title={t(
                    "The update changed the setup form -- click Enable to review and confirm it.",
                    "Das Update hat das Setup-Formular geändert -- auf Aktivieren klicken, um es zu bestätigen.",
                  )}
                >
                  {t("Update pending", "Update ausstehend")}
                </span>
              )}
            </div>
            <span
              className="rounded-full px-1.5 py-0.5 text-xs"
              style={{
                background: `color-mix(in oklab, oklch(0.72 0.14 ${TYPE_HUES[plugin.type] ?? DEFAULT_TYPE_HUE}) 14%, transparent)`,
                color: `oklch(0.6 0.14 ${TYPE_HUES[plugin.type] ?? DEFAULT_TYPE_HUE})`,
              }}
            >
              {typeLabel ? t(typeLabel.en, typeLabel.de) : plugin.type || "—"}
            </span>
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-1">
          {canConfigure && (
            <button
              type="button"
              title={resolveTranslation(
                plugin.setup!.title,
                plugin.setup!.translations?.title,
                lang,
              )}
              onClick={onConfigure}
              className="grid h-7 w-7 shrink-0 place-items-center rounded-md text-muted-foreground transition hover:bg-muted/40 hover:text-foreground"
            >
              <Settings className="h-3.5 w-3.5" />
            </button>
          )}
          <button
            type="button"
            onClick={onDetail}
            title={t("More info", "Weitere Informationen")}
            className="grid h-7 w-7 shrink-0 place-items-center rounded-md text-muted-foreground transition hover:bg-muted/40 hover:text-foreground"
          >
            <Info className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* Clamped -- the full text is one tap away in the detail sheet, not
          hidden, just not eating card height for every Capa at once. */}
      <p className="line-clamp-2 text-sm text-muted-foreground">
        {plugin.summary
          ? resolveTranslation(plugin.summary, plugin.summaryTranslations, lang)
          : t("No description.", "Keine Beschreibung.")}
      </p>

      <div className="mt-auto flex items-center justify-between gap-2 pt-1">
        <ConnectionDot connections={connections} />
        <div className="ml-auto">
          {showRestore ? (
            <button
              type="button"
              onClick={onEnable}
              disabled={busy || !plugin.databaseId}
              className={cn(
                "rounded-md border border-border px-2.5 py-1.5 text-xs transition hover:border-primary/50 hover:text-foreground disabled:opacity-50",
                busy && "opacity-60",
              )}
            >
              {busy ? t("Restoring…", "Wird wiederhergestellt…") : t("Restore", "Wiederherstellen")}
            </button>
          ) : plugin.installed && plugin.installedVersion !== plugin.version ? (
            <button
              type="button"
              onClick={onInstall}
              disabled={busy}
              className={cn(
                "inline-flex shrink-0 items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs text-primary-foreground",
                busy && "opacity-60",
              )}
            >
              <Download className="h-3 w-3" />
              {busy ? t("Updating…", "Wird aktualisiert …") : t("Update", "Aktualisieren")}
            </button>
          ) : plugin.installed && plugin.installationStatus === "enabled" ? (
            <div className="flex shrink-0 items-center gap-1.5">
              <span className="inline-flex items-center gap-1 rounded-md bg-[color:var(--status-running)]/15 px-2 py-1 text-[11px] text-[color:var(--status-running)]">
                <CheckCircle2 className="h-3 w-3" />
                {t("Enabled", "Aktiviert")}
              </span>
              <button
                type="button"
                onClick={onDisable}
                disabled={busy}
                title={t("Disable", "Deaktivieren")}
                className="grid h-6 w-6 place-items-center rounded-md text-muted-foreground transition hover:bg-muted/40 hover:text-foreground disabled:opacity-60"
              >
                <Power className="h-3 w-3" />
              </button>
            </div>
          ) : plugin.installed ? (
            <button
              type="button"
              onClick={onEnable}
              disabled={busy || !plugin.databaseId}
              className={cn(
                "inline-flex shrink-0 items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs text-primary-foreground",
                busy && "opacity-60",
              )}
            >
              <BadgeCheck className="h-3 w-3" />
              {busy ? t("Enabling…", "Wird aktiviert …") : t("Enable", "Aktivieren")}
            </button>
          ) : plugin.valid ? (
            <button
              type="button"
              onClick={onInstall}
              disabled={busy}
              className={cn(
                "inline-flex shrink-0 items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs text-primary-foreground",
                busy && "opacity-60",
              )}
            >
              <Download className="h-3 w-3" />
              {busy ? t("Installing…", "Wird installiert …") : t("Install", "Installieren")}
            </button>
          ) : (
            <span className="inline-flex shrink-0 items-center gap-1 rounded-md bg-[color:var(--status-error)]/15 px-2 py-1 text-[11px] text-[color:var(--status-error)]">
              <AlertTriangle className="h-3 w-3" />
              {t("Invalid", "Ungültig")}
            </span>
          )}
        </div>
      </div>
    </Panel>
  );
}

export function CapaDetailSheet({
  plugin,
  connections,
  onConfigure,
  onClose,
}: {
  plugin: DiscoveredCapa;
  connections: McpConnection[];
  onConfigure: () => void;
  onClose: () => void;
}) {
  const t = useT();
  const { lang } = useLang();
  const typeLabel = TYPE_LABELS[plugin.type];
  const canConfigure = canConfigurePlugin(plugin);

  return (
    <DetailSheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={plugin.label || plugin.name}
      description={
        plugin.summary
          ? resolveTranslation(plugin.summary, plugin.summaryTranslations, lang)
          : t("No description.", "Keine Beschreibung.")
      }
      footer={
        canConfigure ? (
          <button
            type="button"
            onClick={onConfigure}
            className="rounded-md border border-primary/40 bg-primary/10 px-2.5 py-1.5 text-xs text-primary hover:bg-primary/20"
          >
            {resolveTranslation(plugin.setup!.title, plugin.setup!.translations?.title, lang)}
          </button>
        ) : undefined
      }
    >
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <CapaIcon plugin={plugin} />
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded border border-border px-1.5 py-0.5">
            {typeLabel ? t(typeLabel.en, typeLabel.de) : plugin.type || "—"}
          </span>
          <span>v{plugin.version || "—"}</span>
          <span>{plugin.trust}</span>
          {plugin.trust === "first_party" && (
            <BadgeCheck className="h-4 w-4 shrink-0 text-primary" />
          )}
        </div>
      </div>

      {plugin.installed &&
        plugin.installationStatus === "enabled" &&
        plugin.surfaces.length > 0 && (
          <div className="flex flex-wrap gap-1.5 text-[11px]">
            <span className="text-muted-foreground">{t("Available in:", "Verfügbar in:")}</span>
            {plugin.surfaces.map((surface) => (
              <a
                key={surface}
                href={SURFACE_ROUTES[surface] ?? "/capas"}
                className="rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 text-primary hover:bg-primary/20"
              >
                {SURFACE_LABELS[surface] ?? surface}
              </a>
            ))}
          </div>
        )}

      {plugin.installed &&
        plugin.installationStatus === "enabled" &&
        plugin.personalSettings &&
        plugin.personalSettings.location === "approval_channels" && (
          <a
            href="/profile"
            className="flex items-center gap-2 rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-xs text-primary hover:bg-primary/10"
          >
            <UserRound className="h-3.5 w-3.5 shrink-0" />
            {t(
              `${plugin.personalSettings.label} is a personal setting — set it under your Profile.`,
              `${resolveTranslation(
                plugin.personalSettings.label,
                plugin.personalSettings.translations?.label,
                lang,
              )} ist eine persönliche Einstellung — unter deinem Profil einzurichten.`,
            )}
          </a>
        )}

      {!plugin.valid && plugin.error && (
        <p className="rounded-md bg-[color:var(--status-error)]/10 px-2 py-1.5 font-mono text-[11px] text-[color:var(--status-error)]">
          {plugin.error}
        </p>
      )}

      {connections.length > 0 && (
        <div className="space-y-4 border-t border-border pt-4">
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
            {t("Connection", "Verbindung")}
          </div>
          {connections.map((c) => (
            <div key={c.id}>
              {connections.length > 1 && <div className="mb-1.5 text-xs font-medium">{c.name}</div>}
              <McpConnectionBody connection={c} />
            </div>
          ))}
        </div>
      )}
    </DetailSheet>
  );
}
