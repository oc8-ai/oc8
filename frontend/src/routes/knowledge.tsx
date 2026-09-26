import { createFileRoute, Link } from "@tanstack/react-router";
import {
  BookOpen,
  Boxes,
  ChevronRight,
  Clock,
  Database,
  HardDrive,
  FileText,
  Globe,
  Layers,
  Lock,
  Plus,
  RefreshCw,
  Settings,
  ShieldAlert,
  Sparkles,
  Trash2,
  TrendingUp,
  X,
  Zap,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { DetailSheet } from "@/components/detail-sheet";
import { ListToolbar, type ListQueryState } from "@/components/list-toolbar";
import {
  dataSourceKindMeta,
  sensitivityMeta,
  type DataSource,
  type DataSourceKind,
  type KnowledgeBase,
  type SensitivityLevel,
} from "@/lib/mock-data";
import {
  useAgents,
  useDataSources,
  useDepartments,
  useKnowledgeBases,
  useCreateKnowledgeBase,
  useDeleteKnowledgeBase,
  useUpdateKnowledgeBase,
  useUnlinkSourceFromBase,
} from "@/lib/hooks";
import { CredentialPicker } from "@/components/credential-picker";
import {
  JOB_TERMINAL_STATUSES,
  useCreateSource,
  useDeleteSource,
  useIngestionJob,
  useKnowledgeConnectors,
  useKnowledgeVectorIndexes,
  useOAuthConnections,
  useSyncSource,
  useUpdateSource,
} from "@/lib/knowledge-connector-hooks";
import { cn } from "@/lib/utils";
import { useT } from "@/lib/i18n";
import { useConfirm } from "@/hooks/use-confirm";

export const Route = createFileRoute("/knowledge")({
  component: KnowledgePage,
});

type Tab = "sources" | "bases";

// Icons for known connector kinds; anything else falls back to a file glyph.
const KIND_ICON: Record<string, typeof FileText> = {
  website: Globe,
  upload: FileText,
  s3: HardDrive,
  gdrive: HardDrive,
};

const defaultListQueryState: ListQueryState = {
  search: "",
  filters: {},
  groupBy: null,
  includeArchived: false,
  page: 1,
  pageSize: 20,
};

export function KnowledgePage() {
  const t = useT();
  const [tab, setTab] = useState<Tab>("sources");

  // Two independent lists on this page, each with its own <ListToolbar> +
  // server-side search/pagination/archive state (Task 20). Lifted up here
  // (rather than owned inside each tab) because the tab bar's count badges
  // below need both totals regardless of which tab is active.
  const [sourcesQuery, setSourcesQuery] = useState<ListQueryState>(defaultListQueryState);
  const [basesQuery, setBasesQuery] = useState<ListQueryState>(defaultListQueryState);
  const { data: sourcesPage } = useDataSources({
    search: sourcesQuery.search,
    groupBy: sourcesQuery.groupBy,
    includeArchived: sourcesQuery.includeArchived,
    page: sourcesQuery.page,
    pageSize: sourcesQuery.pageSize,
  });
  const { data: basesPage } = useKnowledgeBases({
    search: basesQuery.search,
    groupBy: basesQuery.groupBy,
    includeArchived: basesQuery.includeArchived,
    page: basesQuery.page,
    pageSize: basesQuery.pageSize,
  });
  const fetchedSources = sourcesPage?.items ?? [];
  const fetchedBases = basesPage?.items ?? [];
  const [sources, setSources] = useState<DataSource[]>([]);
  const [bases, setBases] = useState<KnowledgeBase[]>([]);
  useEffect(() => {
    setSources(fetchedSources);
  }, [fetchedSources]);
  useEffect(() => {
    setBases(fetchedBases);
  }, [fetchedBases]);
  const [detailKb, setDetailKb] = useState<string | null>(null);
  const [wizardOpen, setWizardOpen] = useState<null | "source" | "base">(null);

  return (
    <div className="space-y-6">
      {/* Token usage tile — mock daily/monthly consumption */}
      <TokenUsageTile />

      {/* Tab bar */}
      <div className="flex items-center justify-between gap-4">
        <div className="flex gap-1 border-b border-border">
          {[
            {
              id: "sources" as const,
              label: t("Data Sources", "Datenquellen"),
              icon: Database,
              // Total tenant-wide count, not the capped/filtered page
              // length -- mirrors Task 19's departments-list fix.
              count: sourcesPage?.totalCount ?? 0,
            },
            {
              id: "bases" as const,
              label: t("Knowledge Bases", "Wissensdatenbanken"),
              icon: BookOpen,
              count: basesPage?.totalCount ?? 0,
            },
          ].map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={cn(
                "-mb-px inline-flex items-center gap-2 border-b-2 px-4 py-2 text-sm transition",
                tab === t.id
                  ? "border-primary text-primary"
                  : "border-transparent text-muted-foreground hover:text-foreground",
              )}
            >
              <t.icon className="h-4 w-4" />
              {t.label}
              <span className="rounded-full border border-border bg-background/40 px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                {t.count}
              </span>
            </button>
          ))}
        </div>
        <button
          onClick={() => setWizardOpen(tab === "sources" ? "source" : "base")}
          className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:brightness-110 glow-teal"
        >
          <Plus className="h-4 w-4" />
          {tab === "sources"
            ? t("Data source", "Datenquelle")
            : t("Knowledge base", "Wissensdatenbank")}
        </button>
      </div>

      {tab === "sources" && (
        <SourcesTab
          sources={sources}
          setSources={setSources}
          onAdd={() => setWizardOpen("source")}
          queryState={sourcesQuery}
          onQueryStateChange={setSourcesQuery}
          totalCount={sourcesPage?.totalCount ?? 0}
        />
      )}
      {tab === "bases" && (
        <BasesTab
          bases={bases}
          onOpen={(id) => setDetailKb(id)}
          onAdd={() => setWizardOpen("base")}
          queryState={basesQuery}
          onQueryStateChange={setBasesQuery}
          totalCount={basesPage?.totalCount ?? 0}
        />
      )}

      {detailKb && <KbDetailDrawer kbId={detailKb} onClose={() => setDetailKb(null)} />}

      {wizardOpen === "source" && (
        <SourceWizard
          onClose={() => setWizardOpen(null)}
          onCreate={(ds) => {
            setSources((prev) => [ds, ...prev]);
            toast.success("Data source connected", { description: ds.name });
            setWizardOpen(null);
          }}
        />
      )}
      {wizardOpen === "base" && (
        <BaseWizard
          onClose={() => setWizardOpen(null)}
          onCreate={(kb) => {
            setBases((prev) => [kb, ...prev]);
            toast.success("Knowledge base created", { description: kb.name });
            setWizardOpen(null);
          }}
        />
      )}
    </div>
  );
}

// ============================= Sources tab =============================

export function SourcesTab({
  sources,
  setSources,
  onAdd,
  queryState,
  onQueryStateChange,
  totalCount,
}: {
  sources: DataSource[];
  setSources: React.Dispatch<React.SetStateAction<DataSource[]>>;
  onAdd: () => void;
  queryState: ListQueryState;
  onQueryStateChange: (state: ListQueryState) => void;
  totalCount: number;
}) {
  const t = useT();
  const syncSource = useSyncSource();
  const [editingSourceId, setEditingSourceId] = useState<string | null>(null);
  // Task 20 fix-round: resolve the sync-target KB fallback against a
  // *separate*, generously-paged fetch of KnowledgeBases -- not the `bases`
  // prop, which is now the Bases tab's server-search/archive-filterable list
  // (this same task). A user can filter that tab down to zero or one result,
  // switch here, and click Sync; using the filtered `bases` would then
  // resolve to `undefined` (a false "create a KB first" error) or the WRONG
  // `bases[0]` (silently syncing into an unintended KB). Archived KBs are
  // deliberately excluded -- an archived base is not a valid sync target.
  // Mirrors the sourcesLookup fix already applied to BasesTab.
  const { data: syncTargetKbsPage } = useKnowledgeBases({ pageSize: 200 });
  const syncTargetKbs = syncTargetKbsPage?.items ?? [];
  // Ingestion is async now: POST /sync returns a queued job, and this tracks
  // whichever one is currently being polled so its live status can render on
  // the matching source row. `useSyncSource`'s isPending already disables all
  // rows' Sync buttons for the brief POST itself; `activeJob` extends that
  // same "one sync in flight" gating across the job's full queued→terminal
  // lifetime.
  const [activeJob, setActiveJob] = useState<{
    id: string;
    sourceId: string;
    sourceName: string;
  } | null>(null);
  const { data: polledJob } = useIngestionJob(activeJob?.id ?? null);
  useEffect(() => {
    if (!activeJob || !polledJob || !JOB_TERMINAL_STATUSES.has(polledJob.status)) return;
    const fetched = Number(polledJob.stats.fetched ?? 0);
    const ingested = Number(polledJob.stats.ingested ?? 0);
    const chunks = Number(polledJob.stats.chunks ?? 0);
    // A connector-level failure (unknown connector type, denied credentials,
    // an unreachable endpoint...) lands in `stats.error` -- without this, a
    // failed sync and an honestly-empty one both toasted the same silent
    // "0 document(s) ingested, 0 chunk(s)", with no way to tell them apart.
    const fatalError = typeof polledJob.stats.error === "string" ? polledJob.stats.error : null;
    const description =
      fatalError ?? `${fetched} found, ${ingested} document(s) ingested, ${chunks} chunk(s)`;
    if (polledJob.status === "failed") {
      toast.error(`Sync failed · ${activeJob.sourceName}`, { description });
    } else {
      toast.success(`Sync ${polledJob.status} · ${activeJob.sourceName}`, { description });
    }
    setActiveJob(null);
  }, [activeJob, polledJob]);
  const busy = syncSource.isPending || !!activeJob;
  return (
    <div className="space-y-4">
      <Panel className="p-4">
        <ListToolbar
          config={{
            searchPlaceholder: t("Search data sources…", "Datenquellen suchen…"),
            showArchivedToggle: true,
            archivedToggleLabel: t("Show archived", "Archivierte anzeigen"),
          }}
          state={queryState}
          onStateChange={onQueryStateChange}
          totalCount={totalCount}
        />
      </Panel>
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {sources.map((s) => {
          // Defensive: the backend emits kinds (e.g. "upload" from ingested
          // documents) that have no entry in the mock meta/icon maps. Fall
          // back to a neutral label/hue/icon so an unknown kind can't crash
          // the tab.
          const meta = dataSourceKindMeta[s.kind] ?? {
            label: s.kind.replace(/[-_]/g, " "),
            hue: 200,
            description: "",
          };
          const Icon = KIND_ICON[s.kind] ?? FileText;
          const sens = s.sensitivity ? sensitivityMeta[s.sensitivity] : null;
          const kbForSource =
            syncTargetKbs.find((b) => b.sourceIds.includes(s.id)) ?? syncTargetKbs[0];
          // DataSource deletion is irreversible (§12.5.1 -- it reduces the
          // source's KbChunks in the same transaction), so unlike
          // Skills/Agents/Departments there is no restore here: an archived
          // row is read-only -- muted, badged "Deleted", no action buttons.
          const archived = queryState.includeArchived && Boolean(s.deletedAt);
          return (
            <Panel key={s.id} className={cn("p-5", archived && "opacity-60")}>
              <div className="flex items-start gap-3">
                <div
                  className="grid h-10 w-10 shrink-0 place-items-center rounded-md"
                  style={{
                    background: `color-mix(in oklab, oklch(0.72 0.14 ${meta.hue}) 18%, transparent)`,
                    color: `oklch(0.78 0.14 ${meta.hue})`,
                  }}
                >
                  <Icon className="h-5 w-5" />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <h3 className="truncate font-serif text-base leading-tight">{s.name}</h3>
                    <StatusDot
                      connected={s.connected}
                      lastSyncStatus={s.lastSyncStatus}
                      lastSyncError={s.lastSyncError}
                    />
                    {archived && (
                      <span className="rounded-full border border-[color:var(--status-warning)]/40 bg-[color:var(--status-warning)]/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-[color:var(--status-warning)]">
                        {t("Deleted", "Gelöscht")}
                      </span>
                    )}
                  </div>
                  <p className="mt-0.5 truncate text-xs text-muted-foreground">
                    {meta.label} · {s.scope ?? "—"}
                  </p>
                </div>
              </div>

              <dl className="mt-4 grid grid-cols-2 gap-2 text-xs">
                <Stat label="Last sync" value={s.lastSync ?? "—"} />
                <Stat label="Documents" value={s.docCount ? s.docCount.toLocaleString() : "—"} />
                <Stat label="Schedule" value={s.schedule ?? "—"} />
                <Stat
                  label="Sensitivity"
                  value={sens ? <span style={{ color: sens.color }}>{sens.label}</span> : "—"}
                />
              </dl>

              {!archived && s.lastSyncStatus === "failed" && !(activeJob?.sourceId === s.id) && (
                <p className="mt-3 rounded-md border border-[color:var(--status-error)]/30 bg-[color:var(--status-error)]/5 px-2.5 py-1.5 text-xs text-[color:var(--status-error)]">
                  {s.lastSyncError ||
                    t("The last sync failed.", "Der letzte Sync ist fehlgeschlagen.")}
                </p>
              )}

              {!archived && (
                <div className="mt-4 flex items-center gap-2">
                  {s.connected ? (
                    <>
                      {activeJob?.sourceId === s.id && polledJob && (
                        <JobStatusChip status={polledJob.status} />
                      )}
                      <button
                        disabled={busy}
                        onClick={() => {
                          if (!kbForSource) {
                            toast.error("Create a knowledge base first", {
                              description: "Sync needs a knowledge base to ingest into.",
                            });
                            return;
                          }
                          syncSource.mutate(
                            { sourceId: s.id, kbId: kbForSource.id },
                            {
                              onSuccess: (job) => {
                                setSources((prev) =>
                                  prev.map((p) =>
                                    p.id === s.id ? { ...p, lastSync: "just now" } : p,
                                  ),
                                );
                                setActiveJob({ id: job.id, sourceId: s.id, sourceName: s.name });
                                toast.success("Sync started", {
                                  description: `${s.name} — queued`,
                                });
                              },
                              onError: (err) => {
                                toast.error(`Sync failed · ${s.name}`, {
                                  description: err instanceof Error ? err.message : String(err),
                                });
                              },
                            },
                          );
                        }}
                        className="inline-flex flex-1 items-center justify-center gap-1.5 rounded-md border border-border bg-background/40 px-3 py-1.5 text-xs hover:border-primary/50 hover:text-primary disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        <RefreshCw className="h-3.5 w-3.5" />
                        {busy ? "Syncing…" : "Sync"}
                      </button>
                    </>
                  ) : (
                    <button
                      onClick={() => {
                        setSources((prev) =>
                          prev.map((p) =>
                            p.id === s.id
                              ? {
                                  ...p,
                                  connected: true,
                                  lastSync: "just now",
                                  docCount: 0,
                                  schedule: "manual",
                                  sensitivity: "internal",
                                }
                              : p,
                          ),
                        );
                        toast.success(`Connected · ${s.name}`);
                      }}
                      className="inline-flex flex-1 items-center justify-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:brightness-110"
                    >
                      <Zap className="h-3.5 w-3.5" />
                      Connect
                    </button>
                  )}
                  <button
                    className="rounded-md border border-border bg-background/40 px-2 py-1.5 text-xs text-muted-foreground hover:text-foreground"
                    title={t("Configure", "Konfigurieren")}
                    onClick={() => setEditingSourceId(s.id)}
                  >
                    <Settings className="h-3.5 w-3.5" />
                  </button>
                </div>
              )}
            </Panel>
          );
        })}

        <button
          onClick={onAdd}
          className="grid min-h-[220px] place-items-center rounded-xl border-2 border-dashed border-border bg-panel/40 text-sm text-muted-foreground transition hover:border-primary/50 hover:text-primary"
        >
          <div className="flex flex-col items-center gap-2">
            <div className="grid h-10 w-10 place-items-center rounded-full bg-primary/10 text-primary">
              <Plus className="h-5 w-5" />
            </div>
            Add data source
          </div>
        </button>
      </div>

      {editingSourceId &&
        (() => {
          const editing = sources.find((s) => s.id === editingSourceId);
          return editing ? (
            <SourceEditDrawer source={editing} onClose={() => setEditingSourceId(null)} />
          ) : null;
        })()}
    </div>
  );
}

function SourceEditDrawer({ source, onClose }: { source: DataSource; onClose: () => void }) {
  const t = useT();
  const updateSource = useUpdateSource();
  const deleteSource = useDeleteSource();
  const { confirm, ConfirmDialog } = useConfirm();
  const { data: connectors = [] } = useKnowledgeConnectors();
  const [name, setName] = useState(source.name);
  const [sensitivity, setSensitivity] = useState<SensitivityLevel>(
    source.sensitivity ?? "internal",
  );
  // Only credential-typed config keys are editable here -- bucket/prefix/etc.
  // stay out of reach through this drawer, same as the backend's own
  // deliberately-narrow PATCH contract (connector_type and the rest of
  // config are not editable this way).
  const connector = connectors.find((entry) => entry.typeId === source.kind);
  const credentialFields = Object.entries(connector?.configSchema?.properties ?? {}).filter(
    ([, field]) => field.credentialType,
  );
  const [credentialValues, setCredentialValues] = useState<Record<string, string>>({});

  const handleSave = () => {
    if (!name.trim()) return;
    updateSource.mutate(
      {
        sourceId: source.id,
        name: name.trim(),
        classification: sensitivity,
        ...(Object.keys(credentialValues).length > 0 ? { config: credentialValues } : {}),
      },
      {
        onSuccess: () => {
          toast.success(t("Data source updated", "Datenquelle aktualisiert"), {
            description: name.trim(),
          });
          onClose();
        },
        onError: (e: unknown) =>
          toast.error(
            e instanceof Error
              ? e.message
              : t("Could not save changes.", "Änderungen konnten nicht gespeichert werden."),
          ),
      },
    );
  };

  const handleDelete = async () => {
    const ok = await confirm({
      title: t("Delete data source?", "Datenquelle löschen?"),
      description: t(
        `Permanently delete "${source.name}"? This erases everything it has ever ingested, in every knowledge base it feeds -- not just this one -- and cannot be undone.`,
        `"${source.name}" endgültig löschen? Damit werden alle jemals daraus übernommenen Inhalte in jeder Wissensdatenbank gelöscht, die diese Quelle nutzt -- nicht nur hier -- und kann nicht rückgängig gemacht werden.`,
      ),
      confirmLabel: t("Delete", "Löschen"),
      cancelLabel: t("Cancel", "Abbrechen"),
    });
    if (!ok) return;
    deleteSource.mutate(source.id, {
      onSuccess: () => {
        toast.success(t("Data source deleted", "Datenquelle gelöscht"), {
          description: source.name,
        });
        onClose();
      },
      onError: (e: unknown) =>
        toast.error(
          e instanceof Error
            ? e.message
            : t("Could not delete the source.", "Datenquelle konnte nicht gelöscht werden."),
        ),
    });
  };

  return (
    <DetailSheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={source.name}
      description={t("Edit data source", "Datenquelle bearbeiten")}
      footer={
        <>
          <button
            type="button"
            disabled={deleteSource.isPending}
            onClick={handleDelete}
            className="mr-auto inline-flex items-center gap-1.5 rounded-md border border-destructive/40 px-3 py-2 text-sm text-destructive transition hover:bg-destructive/10 disabled:opacity-50"
          >
            <Trash2 className="h-3.5 w-3.5" />
            {deleteSource.isPending ? t("Deleting…", "Wird gelöscht …") : t("Delete", "Löschen")}
          </button>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-border px-3 py-2 text-sm text-muted-foreground transition hover:text-foreground"
          >
            {t("Close", "Schließen")}
          </button>
          <button
            type="button"
            disabled={!name.trim() || updateSource.isPending}
            onClick={handleSave}
            className="rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
          >
            {updateSource.isPending ? t("Saving…", "Speichern …") : t("Save", "Speichern")}
          </button>
        </>
      }
    >
      <div>
        <label className="text-xs font-medium text-muted-foreground">{t("Name", "Name")}</label>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:border-primary/60"
        />
      </div>
      <div className="mt-4">
        <label className="text-xs font-medium text-muted-foreground">
          {t("Sensitivity", "Vertraulichkeit")}
        </label>
        <select
          value={sensitivity}
          onChange={(e) => setSensitivity(e.target.value as SensitivityLevel)}
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:border-primary/60"
        >
          {(Object.keys(sensitivityMeta) as SensitivityLevel[]).map((sv) => (
            <option key={sv} value={sv}>
              {sensitivityMeta[sv].label}
            </option>
          ))}
        </select>
      </div>
      {credentialFields.map(([key, field]) => (
        <div key={key} className="mt-4">
          <label className="text-xs font-medium text-muted-foreground">
            {field.title ?? key.replace(/([A-Z])/g, " $1")}
          </label>
          <div className="mt-1">
            <CredentialPicker
              credentialType={field.credentialType as string}
              value={String(credentialValues[key] ?? source.config?.[key] ?? "")}
              onChange={(id) => setCredentialValues((cur) => ({ ...cur, [key]: id }))}
            />
          </div>
        </div>
      ))}
      {ConfirmDialog}
    </DetailSheet>
  );
}

// ============================= Bases tab =============================

export function BasesTab({
  bases,
  onOpen,
  onAdd,
  queryState,
  onQueryStateChange,
  totalCount,
}: {
  bases: KnowledgeBase[];
  onOpen: (id: string) => void;
  onAdd: () => void;
  queryState: ListQueryState;
  onQueryStateChange: (state: ListQueryState) => void;
  totalCount: number;
}) {
  const t = useT();
  // Pickers over the whole tenant, not a paginated list view.
  const { data: departmentsPage } = useDepartments({ pageSize: 200 });
  const { data: agentsPage } = useAgents({ pageSize: 200 });
  const departments = departmentsPage?.items ?? [];
  const agents = agentsPage?.items ?? [];
  const departmentById = (id: string) => departments.find((d) => d.id === id);
  const agentById = (id: string) => agents.find((a) => a.id === id);
  // Task 20: resolve kb.sourceIds against a *separate*, generously-paged
  // fetch of DataSources -- not the paginated Sources-tab list. That list is
  // now server-search/archive-filterable (this same task), so a KB's linked
  // source can easily sit outside whatever query/page is currently active
  // there; using it here would silently show the name as missing/blank
  // (flagged during Task 15's review). `includeArchived: true` too, so a KB
  // still shows the real name of a since-deleted source rather than "none".
  // Mirrors the department/agent lookups just above.
  const { data: sourcesLookupPage } = useDataSources({ pageSize: 200, includeArchived: true });
  const sourcesLookup = sourcesLookupPage?.items ?? [];
  return (
    <div className="space-y-4">
      <Panel className="p-4">
        <ListToolbar
          config={{
            searchPlaceholder: t("Search knowledge bases…", "Wissensdatenbanken suchen…"),
            showArchivedToggle: true,
            archivedToggleLabel: t("Show archived", "Archivierte anzeigen"),
          }}
          state={queryState}
          onStateChange={onQueryStateChange}
          totalCount={totalCount}
        />
      </Panel>
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {bases.map((kb) => {
          const sens = sensitivityMeta[kb.sensitivity];
          const isRestricted = kb.sensitivity === "restricted";
          const srcNames = kb.sourceIds
            .map((id) => sourcesLookup.find((s) => s.id === id))
            .filter(Boolean) as DataSource[];
          const linkedDepts = kb.linkedDepartments
            .map((d) => departmentById(d)?.name)
            .filter(Boolean) as string[];
          const linkedAgents = kb.linkedAgents
            .map((a) => agentById(a)?.name)
            .filter(Boolean) as string[];
          // KnowledgeBase deletion is irreversible (§12.5.1 -- it reduces the
          // base's KbChunks in the same transaction), so unlike
          // Skills/Agents/Departments there is no restore here: an archived
          // row is read-only -- muted, badged "Deleted", not clickable.
          const archived = queryState.includeArchived && Boolean(kb.deletedAt);

          const cardContent = (
            <Panel
              className={cn(
                "flex flex-col p-5 transition",
                archived ? "opacity-60" : "hover:border-primary/50",
                isRestricted && !archived && "border-[color:var(--status-warning)]/50",
              )}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="flex items-start gap-3">
                  <div
                    className="grid h-10 w-10 shrink-0 place-items-center rounded-md"
                    style={{
                      background: `color-mix(in oklab, ${sens.color} 15%, transparent)`,
                      color: sens.color,
                    }}
                  >
                    <Boxes className="h-5 w-5" />
                  </div>
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <h3 className="truncate font-serif text-base leading-tight">{kb.name}</h3>
                      {archived && (
                        <span className="rounded-full border border-[color:var(--status-warning)]/40 bg-[color:var(--status-warning)]/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-[color:var(--status-warning)]">
                          {t("Deleted", "Gelöscht")}
                        </span>
                      )}
                    </div>
                    <p className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">
                      {kb.description}
                    </p>
                  </div>
                </div>
                {!archived && <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />}
              </div>

              <div className="mt-4 flex flex-wrap items-center gap-1.5">
                <SensPill sens={kb.sensitivity} />
                {kb.localOnly && <MiniPill icon={Lock}>local model</MiniPill>}
                <StatusChip status={kb.status} />
              </div>

              <dl className="mt-4 grid grid-cols-3 gap-2 text-xs">
                <Stat label="Docs" value={kb.docs.toLocaleString()} />
                <Stat label="Chunks" value={(kb.chunks / 1000).toFixed(1) + "k"} />
                <Stat label="Updated" value={kb.updated} />
              </dl>

              {kb.status === "updating" && <SyncProgress className="mt-3" />}

              <div className="mt-4 space-y-1.5 text-xs">
                <Row label="Sources">
                  {srcNames.length === 0 ? (
                    <span className="text-muted-foreground">none</span>
                  ) : (
                    <span className="truncate text-foreground/80">
                      {srcNames.map((s) => s.name.split(" — ")[0]).join(" · ")}
                    </span>
                  )}
                </Row>
                <Row label="Model">
                  <span className="font-mono text-[11px] text-muted-foreground">
                    {kb.embeddingModel.includes("/")
                      ? kb.embeddingModel.split("/")[1]
                      : kb.embeddingModel}
                  </span>
                </Row>
                <Row label="Linked to">
                  <span className="truncate text-foreground/80">
                    {linkedDepts.length > 0 ? linkedDepts.join(", ") : "—"}
                    {linkedAgents.length > 0 && (
                      <span className="text-muted-foreground">
                        {" · "}
                        {linkedAgents.length} agent{linkedAgents.length === 1 ? "" : "s"}
                      </span>
                    )}
                  </span>
                </Row>
              </div>

              {isRestricted && !archived && (
                <div
                  className="mt-4 flex items-start gap-2 rounded-md border p-2 text-[11px]"
                  style={{
                    borderColor: `color-mix(in oklab, ${sens.color} 40%, transparent)`,
                    background: `color-mix(in oklab, ${sens.color} 8%, transparent)`,
                    color: sens.color,
                  }}
                >
                  <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                  <span>Restricted — only cleared roles / local models.</span>
                </div>
              )}
            </Panel>
          );

          return archived ? (
            <div key={kb.id}>{cardContent}</div>
          ) : (
            <div key={kb.id} onClick={() => onOpen(kb.id)} className="cursor-pointer">
              {cardContent}
            </div>
          );
        })}

        <button
          onClick={onAdd}
          className="grid min-h-[260px] place-items-center rounded-xl border-2 border-dashed border-border bg-panel/40 text-sm text-muted-foreground transition hover:border-primary/50 hover:text-primary"
        >
          <div className="flex flex-col items-center gap-2">
            <div className="grid h-10 w-10 place-items-center rounded-full bg-primary/10 text-primary">
              <Plus className="h-5 w-5" />
            </div>
            Create knowledge base
          </div>
        </button>
      </div>
    </div>
  );
}

// ============================= KB detail drawer =============================

export function KbDetailDrawer({ kbId, onClose }: { kbId: string; onClose: () => void }) {
  const t = useT();
  // Detail sheet needs the whole set to find one row by id -- not the
  // paginated list view. `refetch` is how Re-sync below picks up the
  // server's post-ingestion doc/chunk counts once its polled job lands on a
  // terminal status -- no query-key export needed for that.
  const { data: basesPage, refetch: refetchBases } = useKnowledgeBases({ pageSize: 200 });
  const { data: departmentsPage } = useDepartments({ pageSize: 200 });
  const { data: agentsPage } = useAgents({ pageSize: 200 });
  // Every DataSource, archived included, so a since-deleted source that fed
  // this base still resolves to a real name instead of going blank -- same
  // reasoning as BasesTab's own `sourcesLookup` (Task 20's review finding).
  const { data: sourcesLookupPage } = useDataSources({ pageSize: 200, includeArchived: true });
  const bases = basesPage?.items ?? [];
  const departments = departmentsPage?.items ?? [];
  const agents = agentsPage?.items ?? [];
  const sourcesLookup = sourcesLookupPage?.items ?? [];
  const departmentById = (id: string) => departments.find((d) => d.id === id);
  const agentById = (id: string) => agents.find((a) => a.id === id);
  const kb = bases.find((b) => b.id === kbId);

  const updateKb = useUpdateKnowledgeBase();
  const deleteKb = useDeleteKnowledgeBase();
  const unlinkSource = useUnlinkSourceFromBase();
  const syncSource = useSyncSource();
  // Re-sync (below) fires one `syncSource.mutate` per linked source but
  // tracks only the LAST job's id here -- same "good enough" simplification
  // SourcesTab's own `activeJob` makes: every job still runs and ingests
  // server-side regardless of which one this polls, this just decides which
  // one the toast and progress chip describe. The overwhelmingly common case
  // is one source per base anyway.
  const [activeJob, setActiveJob] = useState<{ id: string; label: string } | null>(null);
  const { data: polledJob } = useIngestionJob(activeJob?.id ?? null);
  useEffect(() => {
    if (!activeJob || !polledJob || !JOB_TERMINAL_STATUSES.has(polledJob.status)) return;
    const fetched = Number(polledJob.stats.fetched ?? 0);
    const ingested = Number(polledJob.stats.ingested ?? 0);
    const chunks = Number(polledJob.stats.chunks ?? 0);
    const fatalError = typeof polledJob.stats.error === "string" ? polledJob.stats.error : null;
    const description =
      fatalError ?? `${fetched} found, ${ingested} document(s) ingested, ${chunks} chunk(s)`;
    if (polledJob.status === "failed") {
      toast.error(`${t("Sync failed", "Sync fehlgeschlagen")} · ${activeJob.label}`, {
        description,
      });
    } else {
      toast.success(`${t("Sync", "Sync")} ${polledJob.status} · ${activeJob.label}`, {
        description,
      });
    }
    setActiveJob(null);
    void refetchBases();
  }, [activeJob, polledJob, refetchBases, t]);
  const { confirm, ConfirmDialog } = useConfirm();
  const [name, setName] = useState(kb?.name ?? "");
  const [description, setDescription] = useState(kb?.description ?? "");
  const [embeddingModel, setEmbeddingModel] = useState(kb?.embeddingModel ?? "");
  const [addingSourceId, setAddingSourceId] = useState("");
  // Reset the edit fields whenever the base identity changes (a different KB
  // opened) or the server confirms a save -- never on every keystroke, which
  // would fight the user's own typing.
  const kbName = kb?.name;
  const kbDescription = kb?.description;
  const kbEmbeddingModel = kb?.embeddingModel;
  useEffect(() => {
    if (kbName !== undefined) setName(kbName);
    if (kbDescription !== undefined) setDescription(kbDescription);
    if (kbEmbeddingModel !== undefined) setEmbeddingModel(kbEmbeddingModel);
  }, [kbId, kbName, kbDescription, kbEmbeddingModel]);

  if (!kb) return null;
  const sens = sensitivityMeta[kb.sensitivity];
  const linkedSources = kb.sourceIds
    .map((id) => sourcesLookup.find((s) => s.id === id))
    .filter(Boolean) as DataSource[];
  const unlinkedSources = sourcesLookup.filter((s) => !kb.sourceIds.includes(s.id) && !s.deletedAt);
  // The model can only change while nothing has ever been ingested into this
  // base -- once real chunks exist, old and new embeddings from different
  // models would sit side by side in the same base, and a vector search
  // can't meaningfully compare across them (see UpdateKnowledgeBaseRequest).
  const modelIsLocked = kb.chunks > 0;
  const embeddingModelOptions = Array.from(
    new Set<string>([...LOCAL_EMBEDDING_MODELS, kb.embeddingModel]),
  );
  const dirty =
    name.trim() !== kb.name ||
    description.trim() !== kb.description ||
    (!modelIsLocked && embeddingModel !== kb.embeddingModel);

  const handleSave = () => {
    if (!name.trim()) return;
    updateKb.mutate(
      {
        kbId: kb.id,
        name: name.trim(),
        description: description.trim(),
        ...(!modelIsLocked ? { embeddingModel } : {}),
      },
      {
        onSuccess: () =>
          toast.success(t("Knowledge base updated", "Wissensdatenbank aktualisiert"), {
            description: name.trim(),
          }),
        onError: (e: unknown) =>
          toast.error(
            e instanceof Error
              ? e.message
              : t("Could not save changes.", "Änderungen konnten nicht gespeichert werden."),
          ),
      },
    );
  };

  const handleDelete = async () => {
    const ok = await confirm({
      title: t("Delete knowledge base?", "Wissensdatenbank löschen?"),
      description: t(
        `Permanently delete "${kb.name}"? This erases every document and chunk it holds -- ${kb.docs} document(s), ${kb.chunks} chunk(s) -- and cannot be undone. The sources themselves are not deleted and can still feed other bases.`,
        `"${kb.name}" endgültig löschen? Damit werden alle enthaltenen Dokumente und Chunks gelöscht -- ${kb.docs} Dokument(e), ${kb.chunks} Chunk(s) -- und kann nicht rückgängig gemacht werden. Die Datenquellen selbst werden nicht gelöscht und können weiterhin andere Wissensdatenbanken speisen.`,
      ),
      confirmLabel: t("Delete", "Löschen"),
      cancelLabel: t("Cancel", "Abbrechen"),
    });
    if (!ok) return;
    deleteKb.mutate(kb.id, {
      onSuccess: () => {
        toast.success(t("Knowledge base deleted", "Wissensdatenbank gelöscht"), {
          description: kb.name,
        });
        onClose();
      },
      onError: (e: unknown) =>
        toast.error(
          e instanceof Error
            ? e.message
            : t(
                "Could not delete the knowledge base.",
                "Wissensdatenbank konnte nicht gelöscht werden.",
              ),
        ),
    });
  };

  return (
    <DetailSheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={kb.name}
      description={kb.description}
      footer={
        <button
          type="button"
          disabled={deleteKb.isPending}
          onClick={handleDelete}
          className="mr-auto inline-flex items-center gap-1.5 rounded-md border border-destructive/40 px-3 py-2 text-sm text-destructive transition hover:bg-destructive/10 disabled:opacity-50"
        >
          <Trash2 className="h-3.5 w-3.5" />
          {deleteKb.isPending ? t("Deleting…", "Wird gelöscht …") : t("Delete", "Löschen")}
        </button>
      }
    >
      {/* Name & description */}
      <section>
        <h3 className="mb-2 text-xs uppercase tracking-widest text-muted-foreground">
          {t("Name & description", "Name & Beschreibung")}
        </h3>
        <div className="space-y-3">
          <div>
            <label className="text-xs font-medium text-muted-foreground">{t("Name", "Name")}</label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:border-primary/60"
            />
          </div>
          <div>
            <label className="text-xs font-medium text-muted-foreground">
              {t("Description", "Beschreibung")}
            </label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={2}
              className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:border-primary/60"
            />
          </div>
          <div>
            <label className="text-xs font-medium text-muted-foreground">
              {t("Embedding model", "Embedding-Modell")}
            </label>
            {modelIsLocked ? (
              <>
                <p className="mt-1 rounded-md border border-border bg-background/40 px-3 py-2 text-sm text-muted-foreground">
                  {kb.embeddingModel}
                </p>
                <p className="mt-1 text-xs text-muted-foreground">
                  {t(
                    "This base already has ingested content, so its embedding model can't change -- old and new chunks would no longer be comparable. Delete and recreate the base to use a different model.",
                    "Diese Wissensdatenbank enthält bereits Inhalte, daher kann das Embedding-Modell nicht mehr geändert werden -- alte und neue Chunks wären sonst nicht mehr vergleichbar. Zum Wechseln die Base löschen und neu anlegen.",
                  )}
                </p>
              </>
            ) : (
              <select
                value={embeddingModel}
                onChange={(e) => setEmbeddingModel(e.target.value)}
                title={t("Embedding model", "Embedding-Modell")}
                className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:border-primary/60"
              >
                {embeddingModelOptions.map((model) => (
                  <option key={model} value={model}>
                    {model}
                  </option>
                ))}
              </select>
            )}
          </div>
          {dirty && (
            <div className="flex justify-end">
              <button
                type="button"
                disabled={!name.trim() || updateKb.isPending}
                onClick={handleSave}
                className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
              >
                {updateKb.isPending ? t("Saving…", "Speichern …") : t("Save", "Speichern")}
              </button>
            </div>
          )}
        </div>
      </section>

      {/* Sources */}
      <section>
        <h3 className="mb-2 text-xs uppercase tracking-widest text-muted-foreground">
          {t("Sources", "Datenquellen")}
        </h3>
        <div className="space-y-2">
          {linkedSources.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              {t("No sources linked yet.", "Noch keine Datenquellen verknüpft.")}
            </p>
          ) : (
            linkedSources.map((s) => (
              <div
                key={s.id}
                className="flex items-center justify-between gap-2 rounded-md border border-border bg-panel/60 px-3 py-2 text-xs"
              >
                <span className="truncate">{s.name}</span>
                <button
                  type="button"
                  disabled={unlinkSource.isPending}
                  onClick={async () => {
                    const ok = await confirm({
                      title: t("Remove source?", "Quelle entfernen?"),
                      description: t(
                        `Remove "${s.name}" from this knowledge base? Agents using this base will immediately lose access to what this source contributed.`,
                        `"${s.name}" aus dieser Wissensdatenbank entfernen? Agenten, die diese Basis nutzen, verlieren sofort den Zugriff auf die Inhalte dieser Quelle.`,
                      ),
                      confirmLabel: t("Remove", "Entfernen"),
                      cancelLabel: t("Cancel", "Abbrechen"),
                    });
                    if (!ok) return;
                    unlinkSource.mutate(
                      { kbId: kb.id, sourceId: s.id },
                      {
                        onSuccess: () =>
                          toast.success(t("Source removed", "Quelle entfernt"), {
                            description: s.name,
                          }),
                        onError: (e: unknown) =>
                          toast.error(
                            e instanceof Error
                              ? e.message
                              : t(
                                  "Could not remove source.",
                                  "Quelle konnte nicht entfernt werden.",
                                ),
                          ),
                      },
                    );
                  }}
                  className="rounded-md border border-border bg-background/40 px-2 py-1 text-muted-foreground hover:border-[color:var(--status-error)]/50 hover:text-[color:var(--status-error)] disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {t("Remove", "Entfernen")}
                </button>
              </div>
            ))
          )}

          {unlinkedSources.length > 0 && (
            <div className="flex items-center gap-2 pt-1">
              <select
                value={addingSourceId}
                onChange={(e) => setAddingSourceId(e.target.value)}
                className="min-w-0 flex-1 rounded-md border border-border bg-background px-2 py-1.5 text-xs outline-none focus:border-primary/60"
              >
                <option value="">
                  {t("Add existing source…", "Vorhandene Quelle hinzufügen…")}
                </option>
                {unlinkedSources.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
              <button
                type="button"
                disabled={!addingSourceId || syncSource.isPending}
                onClick={() => {
                  const source = unlinkedSources.find((s) => s.id === addingSourceId);
                  if (!source) return;
                  syncSource.mutate(
                    { sourceId: source.id, kbId: kb.id },
                    {
                      onSuccess: () => {
                        toast.success(t("Sync started", "Sync gestartet"), {
                          description: `${source.name} — ${t("queued", "eingereiht")}`,
                        });
                        setAddingSourceId("");
                      },
                      onError: (e: unknown) =>
                        toast.error(
                          e instanceof Error
                            ? e.message
                            : t("Could not add source.", "Quelle konnte nicht hinzugefügt werden."),
                        ),
                    },
                  );
                }}
                className="shrink-0 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
              >
                {t("Add", "Hinzufügen")}
              </button>
            </div>
          )}
        </div>
      </section>

      {/* Pipeline */}
      <section>
        <div className="mb-2 flex items-center justify-between">
          <h3 className="text-xs uppercase tracking-widest text-muted-foreground">
            Ingestion pipeline
          </h3>
          <div className="flex items-center gap-2 text-xs">
            <SensPill sens={kb.sensitivity} />
            <StatusChip status={kb.status} />
            {activeJob && polledJob && <JobStatusChip status={polledJob.status} />}
            <button
              disabled={syncSource.isPending || !!activeJob || linkedSources.length === 0}
              onClick={() => {
                const targets = linkedSources;
                if (targets.length === 0) return;
                const label = targets.map((s) => s.name).join(", ");
                let lastJobId: string | null = null;
                let failures = 0;
                Promise.all(
                  targets.map((s) =>
                    syncSource
                      .mutateAsync({ sourceId: s.id, kbId: kb.id })
                      .then((job) => {
                        lastJobId = job.id;
                      })
                      .catch(() => {
                        failures += 1;
                      }),
                  ),
                ).then(() => {
                  if (lastJobId) setActiveJob({ id: lastJobId, label });
                  if (failures === targets.length) {
                    toast.error(`${t("Sync failed", "Sync fehlgeschlagen")} · ${label}`);
                  } else {
                    toast.success(t("Sync started", "Sync gestartet"), { description: label });
                  }
                });
              }}
              className="inline-flex items-center gap-1 rounded-md border border-border bg-panel px-2 py-1 hover:border-primary/50 hover:text-primary disabled:cursor-not-allowed disabled:opacity-50"
            >
              <RefreshCw className="h-3 w-3" />
              {activeJob ? t("Syncing…", "Sync läuft…") : t("Re-sync", "Re-sync")}
            </button>
            <Link
              to="/knowledge/bases/$kbId/content"
              params={{ kbId: kb.id }}
              className="text-primary hover:underline"
            >
              {t("Browse content →", "Inhalt durchsuchen →")}
            </Link>
          </div>
        </div>
        <Pipeline updating={!!activeJob} />
        {activeJob && <SyncProgress className="mt-3" />}
      </section>

      {/* Governance */}
      <section>
        <h3 className="mb-2 text-xs uppercase tracking-widest text-muted-foreground">
          Access & governance
        </h3>
        <div className="grid gap-2 rounded-md border border-border bg-panel/60 p-4 text-xs">
          <Row label="Sensitivity">
            <span style={{ color: sens.color }}>{sens.label}</span>
          </Row>
          <Row label="Allowed roles">
            <span>{kb.roles.join(", ")}</span>
          </Row>
          <Row label="Linked departments">
            <span>
              {kb.linkedDepartments.length === 0
                ? "—"
                : kb.linkedDepartments.map((id) => (
                    <Link
                      key={id}
                      to="/departments/$id"
                      params={{ id }}
                      className="mr-1 text-primary hover:underline"
                    >
                      {departmentById(id)?.name}
                    </Link>
                  ))}
            </span>
          </Row>
          <Row label="Linked agents">
            <span>
              {kb.linkedAgents.length === 0
                ? "—"
                : kb.linkedAgents.map((id) => (
                    <Link
                      key={id}
                      to="/agents/$id"
                      params={{ id }}
                      className="mr-1 text-primary hover:underline"
                    >
                      {agentById(id)?.name}
                    </Link>
                  ))}
            </span>
          </Row>
        </div>
      </section>
      {ConfirmDialog}
    </DetailSheet>
  );
}

// ============================= Wizards =============================

function Wizard({
  title,
  step,
  totalSteps,
  onClose,
  children,
  footer,
}: {
  title: string;
  step: number;
  totalSteps: number;
  onClose: () => void;
  children: React.ReactNode;
  footer: React.ReactNode;
}) {
  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="flex w-full max-w-lg flex-col overflow-hidden rounded-xl border border-border bg-panel shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-center justify-between border-b border-border px-5 py-4">
          <div>
            <h3 className="font-serif text-lg leading-tight">{title}</h3>
            <p className="mt-0.5 text-[11px] uppercase tracking-widest text-muted-foreground">
              Step {step} of {totalSteps}
            </p>
          </div>
          <button
            onClick={onClose}
            className="grid h-8 w-8 place-items-center rounded-md border border-border text-muted-foreground hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </header>
        <div className="flex h-1 w-full bg-background/40">
          <div
            className="h-full bg-primary transition-all"
            style={{ width: `${(step / totalSteps) * 100}%` }}
          />
        </div>
        <div className="max-h-[60vh] space-y-4 overflow-y-auto px-5 py-5 text-sm">{children}</div>
        <footer className="flex items-center justify-end gap-2 border-t border-border bg-background/40 px-5 py-3">
          {footer}
        </footer>
      </div>
    </div>
  );
}

export function SourceWizard({
  onClose,
  onCreate,
}: {
  onClose: () => void;
  onCreate: (s: DataSource) => void;
}) {
  const { data: connectors = [], isLoading: connectorsLoading } = useKnowledgeConnectors();
  const { data: oauthConnections = [] } = useOAuthConnections();
  const [step, setStep] = useState(1);
  const [kind, setKind] = useState("");
  const [connectorConfig, setConnectorConfig] = useState<Record<string, unknown>>({});
  const [oauthConnectionId, setOauthConnectionId] = useState("");
  const [schedule, setSchedule] = useState<"hourly" | "daily" | "manual">("daily");
  const [sensitivity, setSensitivity] = useState<SensitivityLevel>("internal");

  const connector = connectors.find((entry) => entry.typeId === kind) ?? connectors[0];
  const schema = connector?.configSchema;
  const connectorLabel = connector?.label ?? kind.replace(/[-_]/g, " ");
  const createSource = useCreateSource();
  const { data: kbsForCreatePage } = useKnowledgeBases({ pageSize: 200 });
  const kbsForCreate = kbsForCreatePage?.items ?? [];
  const targetKbId = kbsForCreate[0]?.id;
  const createKb = useCreateKnowledgeBase();
  const [newKbName, setNewKbName] = useState("");
  const [newKbModel, setNewKbModel] =
    useState<(typeof LOCAL_EMBEDDING_MODELS)[number]>("local/nomic-embed-text");
  // A toast alone was too easy to miss for a validation message the user
  // needs to actually act on (e.g. "prefix is the same as the bucket
  // name") -- this stays visible on whatever step they're on until they
  // retry, instead of fading after a few seconds.
  const [createError, setCreateError] = useState<string | null>(null);

  useEffect(() => {
    if (!connectors.length) return;
    if (!connectors.some((entry) => entry.typeId === kind)) setKind(connectors[0].typeId);
  }, [connectors, kind]);

  useEffect(() => {
    if (!schema) return;
    setConnectorConfig(
      Object.fromEntries(
        Object.entries(schema.properties ?? {}).flatMap(([key, field]) =>
          field.default === undefined ? [] : [[key, field.default]],
        ),
      ),
    );
    setOauthConnectionId("");
  }, [kind, schema]);

  async function finish() {
    if (!connector || !targetKbId) return;
    const required = schema?.required ?? [];
    if (required.some((key) => !String(connectorConfig[key] ?? "").trim())) return;
    if (connector.requiresOauth && !oauthConnectionId) return;
    setCreateError(null);

    // A `credential`-kind field's value is already a credential id by the
    // time we get here -- <CredentialPicker> produced it directly (either
    // an existing credential's id, selected from the dropdown, or a newly
    // created one's id). `connectorConfig` needs no transformation before
    // submitting: no plaintext ever passes through this wizard's own state.
    createSource.mutate(
      {
        connectorType: connector.typeId,
        name: `${connectorLabel} — ${String(
          connectorConfig.name ??
            connectorConfig.url ??
            connectorConfig.folderId ??
            connectorConfig.bucket ??
            "Source",
        )}`,
        config: connectorConfig,
        kbId: targetKbId,
        classification: sensitivity,
        ...(oauthConnectionId ? { oauthConnectionId } : {}),
      },
      {
        onSuccess: (ds) =>
          onCreate({
            id: ds.id,
            kind: connector.typeId as DataSourceKind,
            name: ds.name,
            connected: ds.connected,
            lastSync: ds.lastSync ?? "just now",
            docCount: ds.docCount ?? 0,
            schedule,
            sensitivity,
            scope:
              String(
                connectorConfig.url ?? connectorConfig.folderId ?? connectorConfig.prefix ?? "",
              ) || undefined,
          }),
        onError: (err) => {
          const message = err instanceof Error ? err.message : String(err);
          toast.error("Failed to add data source", { description: message });
          setCreateError(message);
        },
      },
    );
  }

  return (
    <Wizard
      title="Add data source"
      step={step}
      totalSteps={4}
      onClose={onClose}
      footer={
        <>
          {step > 1 && (
            <button
              onClick={() => setStep((s) => s - 1)}
              className="rounded-md border border-border bg-panel px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground"
            >
              Back
            </button>
          )}
          {step < 4 ? (
            <button
              disabled={step === 3 && !targetKbId}
              onClick={() => setStep((s) => s + 1)}
              className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-40 hover:brightness-110"
            >
              Continue
            </button>
          ) : (
            <button
              onClick={finish}
              disabled={
                createSource.isPending ||
                !connector ||
                !targetKbId ||
                (schema?.required ?? []).some(
                  (key) => !String(connectorConfig[key] ?? "").trim(),
                ) ||
                (!!connector?.requiresOauth && !oauthConnectionId)
              }
              className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {createSource.isPending ? "Connecting…" : "Connect"}
            </button>
          )}
        </>
      }
    >
      {step === 1 && (
        <>
          <div className="text-xs uppercase tracking-widest text-muted-foreground">
            Choose a source
          </div>
          <div className="grid grid-cols-2 gap-2">
            {connectors.map((entry) => {
              const Icon = KIND_ICON[entry.typeId as DataSourceKind] ?? FileText;
              const active = kind === entry.typeId;
              return (
                <button
                  key={entry.typeId}
                  onClick={() => setKind(entry.typeId)}
                  className={cn(
                    "flex items-center gap-2 rounded-md border p-3 text-left text-xs transition",
                    active
                      ? "border-primary bg-primary/10"
                      : "border-border bg-background/40 hover:border-primary/40",
                  )}
                >
                  <Icon className="h-4 w-4 text-primary" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <div className="truncate font-medium">
                        {entry.label ?? entry.typeId.replace(/[-_]/g, " ")}
                      </div>
                    </div>
                    <div className="truncate text-[10px] text-muted-foreground">
                      {entry.description ?? "Installed connector"}
                    </div>
                  </div>
                </button>
              );
            })}
            {!connectorsLoading && connectors.length === 0 && (
              <p className="col-span-2 rounded-md border border-dashed border-border p-3 text-xs text-muted-foreground">
                No source connectors are available. Install and enable a connector plugin first.
              </p>
            )}
          </div>
        </>
      )}
      {step === 2 && (
        <>
          <div className="text-xs uppercase tracking-widest text-muted-foreground">
            Connect · {connectorLabel}
          </div>
          <DynamicConnectorFields
            schema={schema}
            config={connectorConfig}
            onChange={(key, value) => {
              setConnectorConfig((current) => ({ ...current, [key]: value }));
              setCreateError(null);
            }}
            oauthProvider={connector?.requiresOauth ?? null}
            oauthConnections={oauthConnections}
            oauthConnectionId={oauthConnectionId}
            onOAuthConnectionChange={setOauthConnectionId}
          />
        </>
      )}
      {step === 3 && (
        <>
          {!targetKbId && (
            <div className="space-y-2 rounded-md border border-[color:var(--status-warning)]/40 bg-[color:var(--status-warning)]/10 p-3">
              <p className="text-xs text-[color:var(--status-warning)]">
                A source needs a knowledge base to sync into — you don't have one yet. Name one now:
              </p>
              <div className="flex gap-2">
                <input
                  value={newKbName}
                  onChange={(e) => setNewKbName(e.target.value)}
                  placeholder="e.g. Company knowledge base"
                  className="flex-1 rounded-md border border-border bg-background/40 px-3 py-1.5 text-xs outline-none focus:border-primary/50"
                />
                <select
                  value={newKbModel}
                  onChange={(e) =>
                    setNewKbModel(e.target.value as (typeof LOCAL_EMBEDDING_MODELS)[number])
                  }
                  title="Embedding model"
                  className="rounded-md border border-border bg-background/40 px-2 py-1.5 text-xs outline-none focus:border-primary/50"
                >
                  {LOCAL_EMBEDDING_MODELS.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
                <button
                  disabled={!newKbName.trim() || createKb.isPending}
                  onClick={() =>
                    createKb.mutate(
                      { name: newKbName.trim(), embeddingModel: newKbModel },
                      {
                        onSuccess: () => {
                          toast.success(`Knowledge base "${newKbName.trim()}" created`);
                          setNewKbName("");
                        },
                        onError: (err) =>
                          toast.error("Could not create knowledge base", {
                            description: err instanceof Error ? err.message : String(err),
                          }),
                      },
                    )
                  }
                  className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-40 hover:brightness-110"
                >
                  {createKb.isPending ? "Creating…" : "Create"}
                </button>
              </div>
            </div>
          )}
          <div>
            <div className="text-xs uppercase tracking-widest text-muted-foreground">
              Sync schedule
            </div>
            <div className="mt-1 flex gap-2">
              {(["hourly", "daily", "manual"] as const).map((s) => (
                <button
                  key={s}
                  onClick={() => setSchedule(s)}
                  className={cn(
                    "flex-1 rounded-md border px-3 py-1.5 text-xs capitalize",
                    schedule === s
                      ? "border-primary bg-primary/10 text-primary"
                      : "border-border bg-background/40 text-muted-foreground",
                  )}
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        </>
      )}
      {step === 4 && (
        <>
          <div className="text-xs uppercase tracking-widest text-muted-foreground">
            Sensitivity label
          </div>
          <div className="grid gap-2">
            {(Object.keys(sensitivityMeta) as SensitivityLevel[]).map((s) => {
              const sm = sensitivityMeta[s];
              const active = sensitivity === s;
              return (
                <button
                  key={s}
                  onClick={() => setSensitivity(s)}
                  className={cn(
                    "flex items-center gap-3 rounded-md border p-3 text-left text-xs transition",
                    active
                      ? "border-primary bg-primary/10"
                      : "border-border bg-background/40 hover:border-primary/40",
                  )}
                >
                  <span
                    className="h-2 w-2 rounded-full"
                    style={{ background: sm.color, boxShadow: `0 0 6px ${sm.color}` }}
                  />
                  <div>
                    <div className="font-medium">{sm.label}</div>
                    <div className="text-[10px] text-muted-foreground">
                      {s === "public" && "Visible to everyone, safe for public models."}
                      {s === "internal" && "Employees only, cloud models allowed."}
                      {s === "confidential" && "Cleared roles, prefer EU-hosted models."}
                      {s === "restricted" && "Cleared roles only, local models required."}
                    </div>
                  </div>
                </button>
              );
            })}
          </div>
        </>
      )}
      {createError && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
          {createError}
        </div>
      )}
    </Wizard>
  );
}

function DynamicConnectorFields({
  schema,
  config,
  onChange,
  oauthProvider,
  oauthConnections,
  oauthConnectionId,
  onOAuthConnectionChange,
}: {
  schema?: {
    properties?: Record<
      string,
      {
        type?: string;
        title?: string;
        description?: string;
        minimum?: number;
        maximum?: number;
        credentialType?: string;
      }
    >;
  };
  config: Record<string, unknown>;
  onChange: (key: string, value: unknown) => void;
  oauthProvider: string | null;
  oauthConnections: { id: string; provider: string; accountLabel: string; status: string }[];
  oauthConnectionId: string;
  onOAuthConnectionChange: (id: string) => void;
}) {
  const fields = Object.entries(schema?.properties ?? {});
  const connections = oauthConnections.filter(
    (connection) => connection.provider === oauthProvider && connection.status === "active",
  );
  return (
    <div className="space-y-3">
      {oauthProvider && (
        <label className="block text-xs text-muted-foreground">
          {oauthProvider} account
          <select
            value={oauthConnectionId}
            onChange={(event) => onOAuthConnectionChange(event.target.value)}
            className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm text-foreground"
          >
            <option value="">Select a connected account…</option>
            {connections.map((connection) => (
              <option key={connection.id} value={connection.id}>
                {connection.accountLabel}
              </option>
            ))}
          </select>
          {!connections.length && (
            <span className="mt-1 block text-[11px] text-[color:var(--status-warning)]">
              No active {oauthProvider} account is connected yet. Configure OAuth first.
            </span>
          )}
        </label>
      )}
      {fields.map(([key, field]) => {
        const label = field.title ?? key.replace(/([A-Z])/g, " $1");
        const value = config[key];
        if (field.credentialType) {
          return (
            <label key={key} className="block text-xs text-muted-foreground">
              {label}
              <div className="mt-1">
                <CredentialPicker
                  credentialType={field.credentialType}
                  value={String(value ?? "")}
                  onChange={(id) => onChange(key, id)}
                />
              </div>
              {field.description && (
                <span className="mt-1 block text-[11px]">{field.description}</span>
              )}
            </label>
          );
        }
        if (field.type === "boolean") {
          return (
            <label key={key} className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={Boolean(value)}
                onChange={(event) => onChange(key, event.target.checked)}
              />
              <span>{label}</span>
            </label>
          );
        }
        return (
          <label key={key} className="block text-xs text-muted-foreground">
            {label}
            <input
              type={field.type === "integer" || field.type === "number" ? "number" : "text"}
              min={field.minimum}
              max={field.maximum}
              value={String(value ?? "")}
              onChange={(event) =>
                onChange(
                  key,
                  field.type === "integer" || field.type === "number"
                    ? Number(event.target.value)
                    : event.target.value,
                )
              }
              className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm text-foreground"
            />
            {field.description && (
              <span className="mt-1 block text-[11px]">{field.description}</span>
            )}
          </label>
        );
      })}
    </div>
  );
}

// Embedding is ALWAYS routed to the local Ollama instance in this deployment
// (ModelRouter.embed() -- Anthropic has no embeddings API, and no cloud
// provider is wired through for embeddings yet, so `google/...` or
// `openai/...` would raise EmbeddingUnavailable the moment a document is
// actually ingested). Not a live discovery call (no endpoint for that exists
// yet), but at least honest about what "local/" means here instead of
// listing providers that can never work.
//
// bge-large was offered here too until live-verified (2026-08-20, via
// POST /api/embed against this deployment's Ollama) to produce 1024-dim
// vectors -- this deployment's kb_chunk.embedding column is a fixed
// Vector(EMBED_DIM), EMBED_DIM = 768. check_embedding_fits()
// (knowledge/ingest.py) refuses the mismatch with a clear IngestionError
// rather than storing a truncated/corrupt vector, so no base already using
// bge-large lost data -- but every document synced into one would fail to
// ingest. Only list models whose output width actually matches EMBED_DIM.
const LOCAL_EMBEDDING_MODELS = ["local/nomic-embed-text"] as const;

export function BaseWizard({
  onClose,
  onCreate,
}: {
  onClose: () => void;
  onCreate: (kb: KnowledgeBase) => void;
}) {
  const t = useT();
  const createKnowledgeBase = useCreateKnowledgeBase();
  const syncSource = useSyncSource();
  const { data: sourcesPage } = useDataSources({ pageSize: 200 });
  const { data: vectorIndexes = [] } = useKnowledgeVectorIndexes();
  const availableSources = (sourcesPage?.items ?? []).filter((s) => s.connected && !s.deletedAt);
  const [step, setStep] = useState(1);
  const [name, setName] = useState("");
  const [mode, setMode] = useState<"ingest" | "connect">("ingest");
  const [model, setModel] =
    useState<(typeof LOCAL_EMBEDDING_MODELS)[number]>("local/nomic-embed-text");
  const [selectedSourceIds, setSelectedSourceIds] = useState<string[]>([]);
  const [indexType, setIndexType] = useState("");
  const [credentialId, setCredentialId] = useState("");
  const [indexConfig, setIndexConfig] = useState<Record<string, unknown>>({});

  const selectedIndex = vectorIndexes.find((v) => v.typeId === indexType) ?? null;

  function toggleSource(id: string) {
    setSelectedSourceIds((prev) =>
      prev.includes(id) ? prev.filter((s) => s !== id) : [...prev, id],
    );
  }

  function finish() {
    if (mode === "connect") {
      if (!selectedIndex || !credentialId) return;
      createKnowledgeBase.mutate(
        {
          name: name.trim(),
          description: "",
          embeddingModel: model,
          indexType: selectedIndex.typeId,
          indexConfig,
          credentialId,
        },
        {
          onSuccess: (kb) => onCreate(kb),
          onError: (error) =>
            toast.error("Could not connect vector index", {
              description: error instanceof Error ? error.message : String(error),
            }),
        },
      );
      return;
    }
    createKnowledgeBase.mutate(
      { name: name.trim(), description: "", embeddingModel: model },
      {
        onSuccess: (kb) => {
          for (const sourceId of selectedSourceIds) {
            syncSource.mutate(
              { sourceId, kbId: kb.id },
              {
                onError: (error) =>
                  toast.error(t("Could not sync a source", "Quelle konnte nicht synct werden"), {
                    description: error instanceof Error ? error.message : String(error),
                  }),
              },
            );
          }
          onCreate(kb);
        },
        onError: (error) =>
          toast.error("Could not create knowledge base", {
            description: error instanceof Error ? error.message : String(error),
          }),
      },
    );
  }

  const totalSteps = 3;
  const canContinueStep1 = Boolean(name.trim());
  const canContinueStep2 =
    mode === "ingest" || (Boolean(indexType) && Boolean(credentialId) && Boolean(selectedIndex));
  const finishLabel =
    mode === "connect"
      ? createKnowledgeBase.isPending
        ? "Connecting…"
        : "Connect index"
      : createKnowledgeBase.isPending
        ? "Creating…"
        : "Create knowledge base";

  return (
    <Wizard
      title={mode === "connect" ? "Connect existing index" : "Create knowledge base"}
      step={step}
      totalSteps={totalSteps}
      onClose={onClose}
      footer={
        <>
          {step > 1 && (
            <button
              onClick={() => setStep((s) => s - 1)}
              className="rounded-md border border-border bg-panel px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground"
            >
              Back
            </button>
          )}
          {step < totalSteps ? (
            <button
              onClick={() => setStep((s) => s + 1)}
              disabled={step === 1 ? !canContinueStep1 : !canContinueStep2}
              className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:brightness-110 disabled:opacity-50"
            >
              Continue
            </button>
          ) : (
            <button
              onClick={finish}
              disabled={createKnowledgeBase.isPending || (mode === "connect" && !canContinueStep2)}
              className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:brightness-110 disabled:opacity-50"
            >
              {finishLabel}
            </button>
          )}
        </>
      }
    >
      {step === 1 && (
        <div className="space-y-4">
          <label className="block">
            <div className="text-xs uppercase tracking-widest text-muted-foreground">Name</div>
            <input
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Sales KB"
              className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
            />
          </label>
          <div>
            <div className="text-xs uppercase tracking-widest text-muted-foreground">
              {t("How should this base get content?", "Wie soll diese Basis Inhalt bekommen?")}
            </div>
            <div className="mt-2 space-y-1.5">
              <label className="flex cursor-pointer items-start gap-2 rounded-md border border-border bg-background/40 px-3 py-2 text-xs hover:border-primary/40">
                <input
                  type="radio"
                  name="kb-mode"
                  checked={mode === "ingest"}
                  onChange={() => setMode("ingest")}
                  className="mt-0.5"
                />
                <span>
                  <span className="font-medium">
                    {t("Ingest sources into oc8", "Quellen in oc8 ingestieren")}
                  </span>
                  <span className="mt-0.5 block text-muted-foreground">
                    {t(
                      "Sync connected sources or upload documents into this base.",
                      "Verbundene Quellen syncen oder Dokumente in diese Basis laden.",
                    )}
                  </span>
                </span>
              </label>
              <label className="flex cursor-pointer items-start gap-2 rounded-md border border-border bg-background/40 px-3 py-2 text-xs hover:border-primary/40">
                <input
                  type="radio"
                  name="kb-mode"
                  checked={mode === "connect"}
                  onChange={() => setMode("connect")}
                  className="mt-0.5"
                  disabled={vectorIndexes.length === 0}
                />
                <span>
                  <span className="font-medium">
                    {t("Connect existing vector index", "Bestehenden Vector-Index verbinden")}
                  </span>
                  <span className="mt-0.5 block text-muted-foreground">
                    {vectorIndexes.length === 0
                      ? t(
                          "Enable a vector-index capa (Qdrant, pgvector) first.",
                          "Aktiviere zuerst eine Vector-Index-Capa (Qdrant, pgvector).",
                        )
                      : t(
                          "Search an existing Qdrant or pgvector collection — query-only, no copy into oc8.",
                          "Eine bestehende Qdrant- oder pgvector-Collection durchsuchen — nur lesen, ohne Kopie in oc8.",
                        )}
                  </span>
                </span>
              </label>
            </div>
          </div>
        </div>
      )}
      {step === 2 && mode === "ingest" && (
        <div>
          <div className="text-xs uppercase tracking-widest text-muted-foreground">
            {t("Data sources (optional)", "Datenquellen (optional)")}
          </div>
          <p className="mt-1 text-xs text-muted-foreground">
            {t(
              "Pick any already-connected sources to sync into this base once it's created. You can add or remove sources later too.",
              "Wähle bereits verbundene Quellen aus, die nach dem Erstellen in diese Basis synct werden. Du kannst Quellen auch später noch hinzufügen oder entfernen.",
            )}
          </p>
          <div className="mt-2 max-h-64 space-y-1.5 overflow-y-auto">
            {availableSources.length === 0 ? (
              <p className="rounded-md border border-border bg-background/40 p-3 text-xs text-muted-foreground">
                {t(
                  "No connected data sources yet — create one first from the Sources tab, or skip this step.",
                  "Noch keine verbundenen Datenquellen — lege zuerst eine im Sources-Tab an, oder überspringe diesen Schritt.",
                )}
              </p>
            ) : (
              availableSources.map((s) => (
                <label
                  key={s.id}
                  className="flex cursor-pointer items-center gap-2 rounded-md border border-border bg-background/40 px-3 py-2 text-xs hover:border-primary/40"
                >
                  <input
                    type="checkbox"
                    checked={selectedSourceIds.includes(s.id)}
                    onChange={() => toggleSource(s.id)}
                    className="h-3.5 w-3.5"
                  />
                  <span className="truncate">{s.name}</span>
                </label>
              ))
            )}
          </div>
        </div>
      )}
      {step === 2 && mode === "connect" && selectedIndex === null && (
        <div>
          <div className="text-xs uppercase tracking-widest text-muted-foreground">
            {t("Index type", "Index-Typ")}
          </div>
          <div className="mt-2 space-y-1.5">
            {vectorIndexes.map((v) => (
              <button
                key={v.typeId}
                type="button"
                onClick={() => {
                  setIndexType(v.typeId);
                  setCredentialId("");
                  const defaults: Record<string, unknown> = {};
                  for (const [key, field] of Object.entries(v.configSchema.properties ?? {})) {
                    if (field.default !== undefined) defaults[key] = field.default;
                  }
                  setIndexConfig(defaults);
                }}
                className={cn(
                  "w-full rounded-md border px-3 py-2 text-left text-xs",
                  indexType === v.typeId
                    ? "border-primary/50 bg-primary/10"
                    : "border-border bg-background/40 hover:border-primary/40",
                )}
              >
                <div className="font-medium">{v.label ?? v.typeId}</div>
                {v.description && (
                  <div className="mt-0.5 text-muted-foreground">{v.description}</div>
                )}
              </button>
            ))}
          </div>
          {indexType && (
            <p className="mt-2 text-xs text-muted-foreground">
              {t("Continue to configure the selected index.", "Weiter, um den Index zu konfigurieren.")}
            </p>
          )}
        </div>
      )}
      {step === 2 && mode === "connect" && selectedIndex !== null && (
        <div className="space-y-3">
          <button
            type="button"
            onClick={() => setIndexType("")}
            className="text-[11px] text-muted-foreground underline"
          >
            {t("Change index type", "Index-Typ ändern")}
          </button>
          <label className="block text-xs text-muted-foreground">
            {t("Credentials", "Zugangsdaten")}
            <div className="mt-1">
              <CredentialPicker
                credentialType={selectedIndex.credentialType}
                value={credentialId}
                onChange={setCredentialId}
              />
            </div>
          </label>
          {Object.entries(selectedIndex.configSchema.properties ?? {}).map(([key, field]) => (
            <label key={key} className="block text-xs text-muted-foreground">
              {field.title ?? key}
              <input
                type="text"
                value={String(indexConfig[key] ?? "")}
                onChange={(e) => setIndexConfig((prev) => ({ ...prev, [key]: e.target.value }))}
                className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm text-foreground"
              />
              {field.description && (
                <span className="mt-1 block text-[11px]">{field.description}</span>
              )}
            </label>
          ))}
        </div>
      )}
      {step === 3 && (
        <>
          <div>
            <div className="text-xs uppercase tracking-widest text-muted-foreground">
              Chunking & embedding
            </div>
            <div className="mt-1 rounded-md border border-border bg-background/40 p-3 text-xs">
              <div className="flex items-center justify-between">
                <span className="font-medium">
                  {mode === "connect"
                    ? t("Must match the remote collection", "Muss zur Remote-Collection passen")
                    : "Recommended defaults"}
                </span>
                <span className="rounded-full border border-primary/40 bg-primary/10 px-2 py-0.5 text-[10px] text-primary">
                  {mode === "connect" ? "required" : "auto"}
                </span>
              </div>
              <p className="mt-1 text-muted-foreground">
                {mode === "connect"
                  ? t(
                      "The embedding model used for queries must be the same family (and dimension) that built the remote collection — otherwise search returns noise.",
                      "Das Embedding-Modell für Abfragen muss dieselbe Familie (und Dimension) haben wie die Remote-Collection — sonst liefert die Suche Müll.",
                    )
                  : `Chunk size 800 · overlap 120 · model ${model}. Good for most document-heavy sources.`}
              </p>
            </div>
          </div>
          <label className="block">
            <div className="text-xs uppercase tracking-widest text-muted-foreground">
              Embedding model
            </div>
            <select
              value={model}
              onChange={(e) => setModel(e.target.value as (typeof LOCAL_EMBEDDING_MODELS)[number])}
              className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
            >
              {LOCAL_EMBEDDING_MODELS.map((m) => (
                <option key={m} value={m}>
                  {m} (on-prem)
                </option>
              ))}
            </select>
            <p className="mt-1 text-[11px] text-muted-foreground">
              {t(
                "Only on-prem models are offered: this deployment routes every embedding through the local Ollama instance, so a cloud model would fail the moment a document is ingested.",
                "Es werden nur On-Prem-Modelle angeboten: dieses Deployment leitet jedes Embedding über die lokale Ollama-Instanz, ein Cloud-Modell würde beim ersten Dokument fehlschlagen.",
              )}
            </p>
          </label>
        </>
      )}
    </Wizard>
  );
}

// ============================= Small parts =============================

function Stat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-md border border-border bg-background/40 px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground">{label}</div>
      <div className="mt-0.5 truncate text-xs font-medium">{value}</div>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="w-24 shrink-0 text-[10px] uppercase tracking-widest text-muted-foreground">
        {label}
      </span>
      <span className="min-w-0 flex-1 truncate">{children}</span>
    </div>
  );
}

function StatusDot({
  connected,
  lastSyncStatus,
  lastSyncError,
}: {
  connected: boolean;
  lastSyncStatus?: "ok" | "failed";
  lastSyncError?: string | null;
}) {
  // A failed sync does NOT move `connected` (transport/credentials still
  // reachable) -- so without checking `lastSyncStatus` separately, a real
  // sync error stayed invisible behind a green "connected" dot.
  const failed = lastSyncStatus === "failed";
  const color = failed
    ? "var(--status-error)"
    : connected
      ? "var(--status-running)"
      : "var(--status-paused)";
  const label = failed ? "sync failed" : connected ? "connected" : "not connected";
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px]"
      title={failed ? (lastSyncError ?? "sync failed") : undefined}
      style={{
        color,
        borderColor: `color-mix(in oklab, ${color} 40%, transparent)`,
        background: `color-mix(in oklab, ${color} 8%, transparent)`,
      }}
    >
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />
      {label}
    </span>
  );
}

// Renders the real ingestion-job status reported by GET /knowledge/jobs/{id}
// (queued|running|succeeded|partial|failed) — no synthetic percentage, just
// whatever the backend last reported while the poll is in flight.
function JobStatusChip({ status }: { status: string }) {
  const meta: Record<string, { color: string; label: string }> = {
    queued: { color: "var(--status-warning)", label: "queued" },
    running: { color: "var(--status-warning)", label: "running…" },
    succeeded: { color: "var(--status-running)", label: "synced" },
    partial: { color: "var(--status-warning)", label: "partial" },
    failed: { color: "var(--status-error)", label: "failed" },
  };
  const m = meta[status] ?? { color: "var(--status-warning)", label: status };
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px]"
      style={{
        color: m.color,
        borderColor: `color-mix(in oklab, ${m.color} 40%, transparent)`,
        background: `color-mix(in oklab, ${m.color} 10%, transparent)`,
      }}
    >
      <span
        className={cn(
          "h-1.5 w-1.5 rounded-full",
          (status === "queued" || status === "running") && "animate-pulse",
        )}
        style={{ background: m.color }}
      />
      {m.label}
    </span>
  );
}

function StatusChip({ status }: { status: "current" | "updating" | "error" }) {
  const meta = {
    current: { color: "var(--status-running)", label: "current" },
    updating: { color: "var(--status-warning)", label: "updating…" },
    error: { color: "var(--status-error)", label: "error" },
  }[status];
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px]"
      style={{
        color: meta.color,
        borderColor: `color-mix(in oklab, ${meta.color} 40%, transparent)`,
        background: `color-mix(in oklab, ${meta.color} 10%, transparent)`,
      }}
    >
      <span
        className={cn("h-1.5 w-1.5 rounded-full", status === "updating" && "animate-pulse")}
        style={{ background: meta.color }}
      />
      {meta.label}
    </span>
  );
}

function SensPill({ sens }: { sens: SensitivityLevel }) {
  const m = sensitivityMeta[sens];
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px]"
      style={{
        color: m.color,
        borderColor: `color-mix(in oklab, ${m.color} 40%, transparent)`,
        background: `color-mix(in oklab, ${m.color} 10%, transparent)`,
      }}
    >
      {sens === "restricted" && <ShieldAlert className="h-2.5 w-2.5" />}
      {m.label}
    </span>
  );
}

function MiniPill({ icon: Icon, children }: { icon: typeof Lock; children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-border bg-background/40 px-1.5 py-0.5 text-[10px] text-muted-foreground">
      <Icon className="h-2.5 w-2.5" />
      {children}
    </span>
  );
}

function Pipeline({ updating }: { updating: boolean }) {
  const steps = [
    { label: "Crawl", icon: Globe },
    { label: "Chunk", icon: Layers },
    { label: "Embed", icon: Sparkles },
    { label: "Vector DB", icon: Database },
  ];
  return (
    <div className="grid grid-cols-4 items-center gap-1 rounded-md border border-border bg-panel/60 p-3">
      {steps.map((s, i) => (
        <div key={s.label} className="flex items-center gap-1">
          <div
            className={cn(
              "flex flex-1 flex-col items-center gap-1 rounded-md border p-2 text-[10px] uppercase tracking-widest",
              updating && i === 2
                ? "border-primary/60 bg-primary/10 text-primary"
                : "border-border bg-background/40 text-muted-foreground",
            )}
          >
            <s.icon
              className={cn("h-4 w-4", updating && i === 2 && "animate-pulse text-primary")}
            />
            {s.label}
          </div>
          {i < 3 && <Clock className="h-3 w-3 shrink-0 text-muted-foreground" />}
        </div>
      ))}
    </div>
  );
}

// ============================= Token usage tile =============================

function TokenUsageTile() {
  // Mock counters that tick up slightly to feel live.
  const [today, setToday] = useState(1_842_500);
  const [month] = useState(38_290_000);
  const budget = 60_000_000;
  const costPerM = 3.2; // €/M tokens blended

  useEffect(() => {
    const iv = setInterval(() => {
      setToday((v) => v + Math.floor(500 + Math.random() * 3500));
    }, 2500);
    return () => clearInterval(iv);
  }, []);

  const monthLive = month + (today - 1_842_500);
  const monthCost = (monthLive / 1_000_000) * costPerM;
  const todayCost = (today / 1_000_000) * costPerM;
  const pct = Math.min(100, (monthLive / budget) * 100);

  return (
    <Panel className="flex flex-wrap items-center gap-6 p-4">
      <div className="flex items-center gap-3">
        <div className="grid h-10 w-10 place-items-center rounded-md bg-primary/15 text-primary">
          <TrendingUp className="h-5 w-5" />
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
            Token consumption
          </div>
          <div className="mt-0.5 text-sm font-medium">oc8 AI Gateway · billed monthly</div>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-6 text-xs">
        <UsageStat
          label="Today"
          primary={<span className="tabular-nums text-foreground">{formatBigTokens(today)}</span>}
          secondary={`€${todayCost.toFixed(2)}`}
          live
        />
        <UsageStat
          label="This month"
          primary={
            <span className="tabular-nums text-foreground">{formatBigTokens(monthLive)}</span>
          }
          secondary={`€${monthCost.toFixed(0)}`}
        />
      </div>

      <div className="ml-auto min-w-[220px] flex-1 max-w-sm">
        <div className="mb-1 flex items-center justify-between text-[10px] uppercase tracking-widest text-muted-foreground">
          <span>Monthly budget</span>
          <span className="font-mono normal-case tracking-normal text-foreground/80">
            {pct.toFixed(0)}% · {formatBigTokens(budget)}
          </span>
        </div>
        <div className="h-1.5 overflow-hidden rounded-full bg-background/60">
          <div
            className="h-full rounded-full bg-primary transition-all duration-700"
            style={{ width: `${pct}%` }}
          />
        </div>
      </div>
    </Panel>
  );
}

function UsageStat({
  label,
  primary,
  secondary,
  live,
}: {
  label: string;
  primary: React.ReactNode;
  secondary: string;
  live?: boolean;
}) {
  return (
    <div>
      <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-widest text-muted-foreground">
        {label}
        {live && (
          <span className="relative inline-flex h-1.5 w-1.5">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-[color:var(--status-running)] opacity-70" />
            <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-[color:var(--status-running)]" />
          </span>
        )}
      </div>
      <div className="mt-0.5 font-serif text-base leading-tight">{primary}</div>
      <div className="text-[11px] text-muted-foreground">
        est. <span className="text-foreground/80">{secondary}</span>
      </div>
    </div>
  );
}

function formatBigTokens(n: number) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
  return String(n);
}

// ============================= Sync progress =============================

function SyncProgress({ className }: { className?: string }) {
  const [pct, setPct] = useState(8);
  const ref = useRef(pct);
  useEffect(() => {
    const iv = setInterval(() => {
      // Ease toward ~95% then hold — signals "in-flight".
      const next = ref.current + Math.max(0.5, (95 - ref.current) * 0.08);
      ref.current = next > 95 ? 95 : next;
      setPct(ref.current);
    }, 350);
    return () => clearInterval(iv);
  }, []);

  return (
    <div className={cn("space-y-1", className)}>
      <div className="flex items-center justify-between text-[10px] uppercase tracking-widest text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <RefreshCw className="h-3 w-3 animate-spin text-primary" />
          Syncing
        </span>
        <span className="font-mono normal-case tracking-normal text-foreground/70">
          {pct.toFixed(0)}%
        </span>
      </div>
      <div className="relative h-1 overflow-hidden rounded-full bg-background/60">
        <div
          className="h-full rounded-full bg-primary/90 transition-all duration-300"
          style={{
            width: `${pct}%`,
            backgroundImage:
              "linear-gradient(90deg, color-mix(in oklab, var(--primary) 60%, transparent), var(--primary), color-mix(in oklab, var(--primary) 60%, transparent))",
          }}
        />
        <div className="pointer-events-none absolute inset-0 animate-pulse bg-primary/10" />
      </div>
    </div>
  );
}
