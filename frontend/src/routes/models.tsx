import { createFileRoute } from "@tanstack/react-router";
import {
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  Cpu,
  Cloud,
  HardDrive,
  KeyRound,
  Pencil,
  Plus,
  RefreshCw,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import { useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { ChatGptSubscriptionPicker } from "@/components/chatgpt-subscription-picker";
import { CredentialPicker } from "@/components/credential-picker";
import {
  extraToPairs,
  pairsToExtra,
  RawParamsEditor,
  type RawParamPair,
} from "@/components/raw-params-editor";
import { agents } from "@/lib/mock-data";
import { cn } from "@/lib/utils";
import { useT } from "@/lib/i18n";
import { useMay } from "@/lib/governance-hooks";
import { useConfirm } from "@/hooks/use-confirm";
import {
  useCreateModel,
  useCreateSecret,
  useCredentials,
  useDeleteCredential,
  useDeleteModel,
  useDiscoverModels,
  useModelProviders,
  useModels,
  useTestModel,
  useUpdateModel,
  type ModelDTO,
  type ModelProviderDTO,
  type ModelWriteBody,
} from "@/lib/hooks";
import {
  useCreateModelPrice,
  useDeactivateModelPrice,
  useModelPrices,
  type ModelPrice,
} from "@/lib/model-prices-hooks";

export const Route = createFileRoute("/models")({
  component: ModelsPage,
});

interface ProviderGroup {
  canonical: string;
  locality: string;
  available: boolean;
  configs: ModelDTO[];
}

// The one provider whose credential is a personal ChatGPT subscription (an
// OAuth device-code login) instead of an API key -- see
// `ChatGptSubscriptionPicker`'s doc comment. `ModelDTO` doesn't carry
// `credentialType`, only `credentialId` -- but this provider is ALWAYS
// subscription-based by construction (Task 7 only ever wires this canonical
// name to that one credential type), so every render site (the models table,
// ProviderCard, the wizard's step-1 tile list, and the agent detail page's
// Assigned-LLM panel) gates on `provider === SUBSCRIPTION_PROVIDER`, never on
// a credential-type lookup. Exported for that last one, which lives in
// `routes/agents.$id.tsx`.
export const SUBSCRIPTION_PROVIDER = "openai_chatgpt";

/** Virtual complexity router — not a real LLM adapter. */
export const AUTO_PROVIDER = "auto";

// Every adapter merges ModelParams.extra into its outbound payload EXCEPT
// these two (modelrouter/adapters/ollama.py + chatgpt_subscription.py each
// build their own payload and never look at params.extra) -- everything else,
// including anthropic/openai/openai_compatible AND any plugin-contributed
// provider built on OpenAICompatibleAdapter (e.g. capas/openrouter_provider,
// capas/opaas_ai_provider -- both registered via `_openai_common.build_payload`
// under the hood), forwards it. A denylist of the two core built-ins, rather
// than an allowlist, is what keeps this software-neutral: core has no way to
// enumerate plugin provider names (core-must-be-software-neutral), so an
// allowlist would silently hide the editor for every plugin provider even
// though its adapter supports it -- exactly the OpenRouter case this feature
// was built for. Exported for the agent detail page's Assigned-LLM panel,
// same reuse reason as SUBSCRIPTION_PROVIDER above. Auto is included because
// it is a router policy, not a completion adapter.
const RAW_PARAMS_UNSUPPORTED_PROVIDERS = new Set(["ollama", SUBSCRIPTION_PROVIDER, AUTO_PROVIDER]);
export function supportsRawParams(provider: string): boolean {
  return !RAW_PARAMS_UNSUPPORTED_PROVIDERS.has(provider);
}

export function isAutoProvider(provider: string): boolean {
  return provider === AUTO_PROVIDER;
}

/** Label for agent selects / pickers — Auto reads as a router, not an LLM. */
export function modelAssignmentLabel(m: ModelDTO): string {
  if (isAutoProvider(m.provider)) {
    return `${m.displayName || m.name || "Auto"} · Auto router`;
  }
  return `${m.displayName || m.model} · ${m.provider}`;
}

export function AutoRouterBadge() {
  const t = useT();
  return (
    <span
      title={t(
        "Complexity router — picks among the tier models you configure (fast / balanced / strong).",
        "Komplexitäts-Router — wählt unter den konfigurierten Tier-Modellen (fast / balanced / strong).",
      )}
      className="rounded-full border border-primary/40 bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary"
    >
      {t("Auto router", "Auto-Router")}
    </span>
  );
}

// Persistent, non-dismissable reminder wherever a model or provider tile is
// backed by a personal ChatGPT subscription: scheduled/automated runs are
// blocked server-side for this credential type (Tasks 9-11's enforcement).
// This is the UI half of that risk-labeling requirement, distinct from the
// server-side block itself -- a plain span with no close affordance, so it
// can't be dismissed away and forgotten.
export function SubscriptionRiskBadge() {
  const t = useT();
  return (
    <span
      title={t(
        "Connected via a personal ChatGPT subscription — scheduled/automated runs are blocked server-side.",
        "Verbunden über ein persönliches ChatGPT-Abo — geplante/automatisierte Läufe sind serverseitig gesperrt.",
      )}
      className="rounded-full border border-[color:var(--status-warning)]/40 bg-[color:var(--status-warning)]/10 px-1.5 py-0.5 text-[10px] text-[color:var(--status-warning)]"
    >
      {t("Personal · manual only", "Persönlich · nur manuell")}
    </span>
  );
}

export function ModelsPage() {
  const t = useT();
  // `model:manage`, not `model:view`: this page's buttons DECLARE a provider,
  // and `pdp.py` believes a declaration that a cloud endpoint is "local". The
  // read side is `model:view` and it is what the sidebar gates on.
  const may = useMay();
  const mayManage = may("model:manage");
  const { data: models = [] } = useModels();
  const { data: providers = [] } = useModelProviders();
  const [wizardOpen, setWizardOpen] = useState(false);
  const [modelForm, setModelForm] = useState<ModelDTO | "new" | null>(null);
  const deleteModel = useDeleteModel();
  const testModel = useTestModel();
  const [testingId, setTestingId] = useState<string | null>(null);
  const { confirm, ConfirmDialog } = useConfirm();

  const handleTest = async (modelId: string) => {
    setTestingId(modelId);
    try {
      await testModel.mutateAsync(modelId);
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : t("Could not run the test", "Test konnte nicht ausgeführt werden"),
      );
    } finally {
      setTestingId(null);
    }
  };

  const providerGroups = useMemo<ProviderGroup[]>(() => {
    const byProvider = new Map<string, ModelDTO[]>();
    for (const m of models) {
      const arr = byProvider.get(m.provider) ?? [];
      arr.push(m);
      byProvider.set(m.provider, arr);
    }
    const canonicals = new Set<string>([
      ...providers.map((p) => p.canonical),
      ...byProvider.keys(),
    ]);
    return Array.from(canonicals)
      .map((canonical) => {
        const configs = byProvider.get(canonical) ?? [];
        const meta = providers.find((p) => p.canonical === canonical);
        return {
          canonical,
          locality: meta?.locality ?? configs[0]?.locality ?? "cloud",
          available: meta?.available ?? false,
          configs,
        };
      })
      .filter(
        // Only providers actually connected -- a completion key set (or a
        // local provider, always "available"), or at least one model still
        // registered under it. Every OTHER built-in/plugin provider the
        // tenant could theoretically use (available_provider_entries on the
        // backend) shows up in useModelProviders() regardless of whether
        // anyone ever configured it -- without this filter, an untouched
        // provider rendered a permanent "key missing" tile with its own
        // inline credential picker, a second path to connect a provider
        // that bypassed the wizard entirely. Connecting a NEW provider now
        // only happens through "Add model provider".
        // Auto is always "available" server-side (no key) but is configured
        // via New model, not as a Connected LLM tile — only show it once
        // an Auto ModelConfig exists.
        (g) =>
          g.canonical === AUTO_PROVIDER
            ? g.configs.length > 0
            : g.available || g.configs.length > 0,
      );
  }, [models, providers]);

  const stats = useMemo(() => {
    const activeProviders = providerGroups.filter((g) => g.configs.length > 0).length;
    const totalConfigs = models.length;
    const localConfigs = models.filter((m) => m.locality === "local").length;
    return { activeProviders, totalConfigs, localConfigs };
  }, [providerGroups, models]);

  const handleDelete = async (m: ModelDTO) => {
    const ok = await confirm({
      title: t("Delete model?", "Modell löschen?"),
      description: t(
        `Delete ${m.name}? This cannot be undone.`,
        `${m.name} löschen? Dies kann nicht rückgängig gemacht werden.`,
      ),
      confirmLabel: t("Delete", "Löschen"),
      cancelLabel: t("Cancel", "Abbrechen"),
    });
    if (!ok) return;
    try {
      await deleteModel.mutateAsync(m.id);
      toast.success(t(`${m.name} deleted`, `${m.name} gelöscht`));
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : t(
              "Could not delete — it may be assigned to agents.",
              "Konnte nicht gelöscht werden — möglicherweise Agenten zugewiesen.",
            ),
      );
    }
  };

  return (
    <div className="space-y-6">
      {/* Providers */}
      <Panel className="p-5">
        <header className="mb-4 flex flex-wrap items-start justify-between gap-3">
          <div className="flex items-start gap-3">
            <div className="grid h-9 w-9 place-items-center rounded-md bg-primary/15 text-primary">
              <KeyRound className="h-4 w-4" />
            </div>
            <div>
              <div className="font-serif text-lg leading-tight">
                {t("Connected LLMs", "Verbundene LLMs")}
              </div>
              <p className="mt-0.5 text-xs text-muted-foreground">
                {t(
                  "Your model providers. Add more via the wizard — cloud accounts or local servers.",
                  "Ihre Modellanbieter. Weitere über den Assistenten hinzufügen — Cloud-Konten oder lokale Server.",
                )}
              </p>
              <div className="mt-2 flex flex-wrap items-center gap-3 text-[11px] text-muted-foreground">
                <span className="inline-flex items-center gap-1">
                  <CheckCircle2 className="h-3 w-3 text-[color:var(--status-running)]" />
                  {stats.activeProviders} {t("configured", "konfiguriert")} · {stats.totalConfigs}{" "}
                  {t("models", "Modelle")}
                </span>
                <span className="inline-flex items-center gap-1">
                  <HardDrive className="h-3 w-3" /> {stats.localConfigs} {t("local", "lokal")}
                </span>
              </div>
            </div>
          </div>
          {mayManage && (
            <button
              type="button"
              onClick={() => setWizardOpen(true)}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:brightness-110 glow-teal"
            >
              <Plus className="h-4 w-4" /> {t("Add model provider", "Modellanbieter hinzufügen")}
            </button>
          )}
        </header>

        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {providerGroups.map((g) => (
            <ProviderCard key={g.canonical} group={g} mayManage={mayManage} />
          ))}
          {mayManage && (
            <button
              type="button"
              onClick={() => setWizardOpen(true)}
              className="group flex min-h-[180px] flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border bg-background/20 p-4 text-muted-foreground transition hover:border-primary/50 hover:bg-primary/5 hover:text-primary"
            >
              <div className="grid h-10 w-10 place-items-center rounded-full border border-dashed border-current">
                <Plus className="h-5 w-5" />
              </div>
              <div className="text-sm font-medium">
                {t("Add another provider", "Weiteren Anbieter hinzufügen")}
              </div>
              <div className="text-[11px] text-muted-foreground group-hover:text-primary/80">
                {t("Cloud or local — 3 quick steps", "Cloud oder lokal — 3 schnelle Schritte")}
              </div>
            </button>
          )}
          {providerGroups.length === 0 && (
            <div className="rounded-lg border border-dashed border-border bg-background/20 p-6 text-center text-sm text-muted-foreground sm:col-span-2 xl:col-span-2">
              {t(
                "No providers configured yet. Start with the wizard.",
                "Noch keine Anbieter konfiguriert. Mit dem Assistenten starten.",
              )}
            </div>
          )}
        </div>
      </Panel>

      {/* Existing models table */}
      <Panel className="overflow-hidden">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-5 py-3">
          <div>
            <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
              {t("Available models", "Verfügbare Modelle")}
            </div>
            <div className="font-serif text-base">
              {t("Assigned to agents", "Agenten zugewiesen")}
            </div>
          </div>
          <div className="flex items-center gap-3">
            <span className="text-[11px] text-muted-foreground">
              {t(
                "Models from connected providers appear here automatically.",
                "Modelle verbundener Anbieter erscheinen hier automatisch.",
              )}
            </span>
            {mayManage && (
              <button
                type="button"
                onClick={() => setModelForm("new")}
                className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:brightness-110 glow-teal"
              >
                <Plus className="h-3.5 w-3.5" /> {t("New model", "Neues Modell")}
              </button>
            )}
          </div>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs uppercase tracking-wider text-muted-foreground">
                <th className="px-5 py-3 font-medium">{t("Model", "Modell")}</th>
                <th className="px-5 py-3 font-medium">{t("Provider", "Anbieter")}</th>
                <th className="px-5 py-3 font-medium">{t("Status", "Status")}</th>
                <th className="px-5 py-3 font-medium">{t("Latency", "Latenz")}</th>
                <th className="px-5 py-3 font-medium">{t("Cost", "Kosten")}</th>
                <th className="px-5 py-3 font-medium">{t("Assigned to", "Zugewiesen an")}</th>
                <th className="px-5 py-3 font-medium text-right">{t("Actions", "Aktionen")}</th>
              </tr>
            </thead>
            <tbody>
              {models.length === 0 && (
                <tr>
                  <td colSpan={7} className="px-5 py-8 text-center text-sm text-muted-foreground">
                    {t("No models configured yet.", "Noch keine Modelle konfiguriert.")}
                  </td>
                </tr>
              )}
              {models.map((m) => {
                const color =
                  m.status === "healthy"
                    ? "var(--status-running)"
                    : m.status === "degraded"
                      ? "var(--status-warning)"
                      : m.status === "unknown"
                        ? "var(--muted-foreground)"
                        : "var(--status-error)";
                return (
                  <tr key={m.id} className="border-b border-border/60 last:border-none">
                    <td className="px-5 py-4">
                      <div className="flex items-center gap-2 font-medium">
                        <Cpu className="h-4 w-4 text-primary" />
                        {m.name}
                        {isAutoProvider(m.provider) && <AutoRouterBadge />}
                      </div>
                      <div className="mt-0.5 text-xs text-muted-foreground">{m.note}</div>
                    </td>
                    <td className="px-5 py-4">
                      <div className="flex flex-wrap items-center gap-1">
                        <span className="rounded-full border border-border bg-background/40 px-2 py-0.5 text-xs">
                          {isAutoProvider(m.provider) ? t("Auto", "Auto") : m.provider}
                        </span>
                        {m.provider === SUBSCRIPTION_PROVIDER && <SubscriptionRiskBadge />}
                      </div>
                    </td>
                    <td className="px-5 py-4">
                      <span
                        className="inline-flex items-center gap-1.5 text-xs"
                        style={{ color }}
                        title={
                          m.healthCheckedAt
                            ? t(
                                `Checked ${new Date(m.healthCheckedAt).toLocaleString()}`,
                                `Geprüft am ${new Date(m.healthCheckedAt).toLocaleString()}`,
                              )
                            : t(
                                "Never tested — click Test to check.",
                                "Noch nie getestet — auf Test klicken zum Prüfen.",
                              )
                        }
                      >
                        <span
                          className="h-1.5 w-1.5 rounded-full"
                          style={{ background: color, boxShadow: `0 0 8px ${color}` }}
                        />
                        {m.status}
                      </span>
                      {m.healthError && (
                        <div className="mt-0.5 max-w-xs text-[11px] text-muted-foreground">
                          {m.healthError}
                        </div>
                      )}
                    </td>
                    <td className="px-5 py-4 font-mono text-xs">{m.latency}</td>
                    <td className="px-5 py-4">
                      <div className="flex gap-0.5">
                        {[1, 2, 3].map((n) => (
                          <span
                            key={n}
                            className={
                              m.costTier.length >= n
                                ? "h-4 w-1.5 rounded-sm bg-primary/70"
                                : "h-4 w-1.5 rounded-sm bg-border"
                            }
                          />
                        ))}
                      </div>
                    </td>
                    <td className="px-5 py-4">
                      <div className="flex flex-wrap gap-1">
                        {m.assignedTo.map((id) => {
                          const a = agents.find((x) => x.id === id);
                          if (!a) return null;
                          return (
                            <span
                              key={id}
                              className="inline-flex items-center gap-1 rounded-full border border-border bg-background/30 px-2 py-0.5 text-[10px]"
                            >
                              <span
                                className="h-1.5 w-1.5 rounded-full"
                                style={{ background: a.avatarColor }}
                              />
                              {a.name}
                            </span>
                          );
                        })}
                      </div>
                    </td>
                    <td className="px-5 py-4 text-right">
                      {mayManage && (
                        <div className="flex items-center justify-end gap-1.5">
                          <button
                            type="button"
                            onClick={() => handleTest(m.id)}
                            disabled={testingId === m.id}
                            className="inline-flex items-center gap-1 rounded-md border border-border bg-background/40 px-2 py-1 text-xs text-muted-foreground hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
                          >
                            <RefreshCw
                              className={cn("h-3 w-3", testingId === m.id && "animate-spin")}
                            />{" "}
                            {t("Test", "Testen")}
                          </button>
                          <button
                            type="button"
                            onClick={() => setModelForm(m)}
                            className="inline-flex items-center gap-1 rounded-md border border-border bg-background/40 px-2 py-1 text-xs text-muted-foreground hover:text-foreground"
                          >
                            <Pencil className="h-3 w-3" /> {t("Edit", "Bearbeiten")}
                          </button>
                          <button
                            type="button"
                            onClick={() => handleDelete(m)}
                            className="inline-flex items-center gap-1 rounded-md border border-border bg-background/40 px-2 py-1 text-xs text-muted-foreground hover:text-[color:var(--status-error)]"
                          >
                            <Trash2 className="h-3 w-3" /> {t("Delete", "Löschen")}
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Panel>

      {modelForm && (
        <ModelFormDialog
          editing={modelForm === "new" ? null : modelForm}
          onClose={() => setModelForm(null)}
        />
      )}

      <ModelPricingPanel />

      {wizardOpen && <AddProviderWizard onClose={() => setWizardOpen(false)} />}
      {ConfirmDialog}
    </div>
  );
}

// Reconciliation (Cost Center §Part C) only exists for Anthropic and OpenAI —
// Mistral (openai_compatible) and local servers have no admin-key cost API,
// so no affordance is shown for them at all.
const RECONCILIATION_PROVIDERS = new Set(["anthropic", "openai"]);

export function ProviderCard({ group, mayManage }: { group: ProviderGroup; mayManage: boolean }) {
  const t = useT();
  const isLocal = group.locality === "local";
  const isAuto = group.canonical === AUTO_PROVIDER;
  const KindIcon = isLocal ? HardDrive : Cloud;
  const agentCount = group.configs.reduce((sum, c) => sum + c.assignedTo.length, 0);
  const hue = hashHue(group.canonical);
  const supportsReconciliation = RECONCILIATION_PROVIDERS.has(group.canonical);

  const credentialType = `${group.canonical}_api_key`;
  const { data: credentials = [] } = useCredentials(isLocal || isAuto ? undefined : credentialType);
  const deleteModel = useDeleteModel();
  const deleteCredential = useDeleteCredential();
  const qc = useQueryClient();
  const { confirm, ConfirmDialog: RemoveConfirmDialog } = useConfirm();
  const [removing, setRemoving] = useState(false);

  const handleRemoveProvider = async () => {
    if (agentCount > 0) {
      toast.error(
        t(
          "Unassign this provider's models from every agent before removing it.",
          "Entferne die Modelle dieses Anbieters erst von allen Agenten, bevor du ihn löschst.",
        ),
      );
      return;
    }
    const ok = await confirm({
      title: t("Remove provider?", "Anbieter entfernen?"),
      description:
        group.configs.length > 0
          ? t(
              `This deletes ${group.canonical} and its ${group.configs.length} model(s). This cannot be undone.`,
              `Das löscht ${group.canonical} und die ${group.configs.length} zugehörigen Modelle. Dies kann nicht rückgängig gemacht werden.`,
            )
          : t(
              `Remove ${group.canonical}? This cannot be undone.`,
              `${group.canonical} entfernen? Dies kann nicht rückgängig gemacht werden.`,
            ),
      confirmLabel: t("Remove", "Entfernen"),
      cancelLabel: t("Cancel", "Abbrechen"),
    });
    if (!ok) return;
    setRemoving(true);
    try {
      await Promise.all(group.configs.map((c) => deleteModel.mutateAsync(c.id)));
      if (!isLocal && !isAuto && credentials[0]) {
        await deleteCredential.mutateAsync(credentials[0].id);
      }
      // Neither useDeleteModel nor useDeleteCredential invalidates this --
      // it's a separate query (see CompletionKeyPicker's own comment above)
      // and without it the now-gone provider's tile would linger until some
      // unrelated refetch happened to run.
      qc.invalidateQueries({ queryKey: ["model-providers"] });
      toast.success(t(`${group.canonical} removed`, `${group.canonical} entfernt`));
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : t("Something went wrong", "Etwas ist schiefgelaufen"),
      );
    } finally {
      setRemoving(false);
    }
  };

  return (
    <div className="flex flex-col gap-3 rounded-lg border border-border bg-background/30 p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-3 min-w-0">
          <div
            className="grid h-10 w-10 shrink-0 place-items-center rounded-md font-serif text-black"
            style={{ background: `oklch(0.75 0.14 ${hue})` }}
          >
            {group.canonical[0]?.toUpperCase()}
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="truncate font-medium">
                {isAuto ? t("Auto", "Auto") : group.canonical}
              </span>
              {isAuto ? (
                <AutoRouterBadge />
              ) : (
                <span className="inline-flex items-center gap-1 rounded-full border border-border bg-background/40 px-1.5 py-0.5 text-[10px] text-muted-foreground">
                  <KindIcon className="h-2.5 w-2.5" /> {isLocal ? "local" : "cloud"}
                </span>
              )}
              {group.canonical === SUBSCRIPTION_PROVIDER && <SubscriptionRiskBadge />}
            </div>
            <div className="text-[11px] text-muted-foreground">
              {isAuto
                ? t(
                    "Routes each turn to a configured tier model",
                    "Leitet jeden Zug an ein konfiguriertes Tier-Modell weiter",
                  )
                : `${agentCount} agent${agentCount === 1 ? "" : "s"} using this provider`}
            </div>
          </div>
        </div>
        {isAuto ? (
          <span className="rounded-full border border-border bg-background/40 px-1.5 py-0.5 text-[10px] text-muted-foreground">
            {t("no key", "kein Schlüssel")}
          </span>
        ) : isLocal ? (
          <span className="rounded-full border border-border bg-background/40 px-1.5 py-0.5 text-[10px] text-muted-foreground">
            local
          </span>
        ) : group.available ? (
          <span className="inline-flex items-center gap-1 rounded-full border border-[color:var(--status-running)]/40 bg-[color:var(--status-running)]/10 px-1.5 py-0.5 text-[10px] text-[color:var(--status-running)]">
            <CheckCircle2 className="h-2.5 w-2.5" /> key set
          </span>
        ) : (
          <span className="rounded-full border border-[color:var(--status-warning)]/40 bg-[color:var(--status-warning)]/10 px-1.5 py-0.5 text-[10px] text-[color:var(--status-warning)]">
            key missing
          </span>
        )}
      </div>

      <div className="flex flex-wrap gap-1">
        {group.configs.slice(0, 3).map((c) => (
          <span
            key={c.id}
            className="rounded-full border border-border bg-background/40 px-1.5 py-0.5 font-mono text-[10px]"
          >
            {isAuto ? c.displayName || c.name || "Auto" : c.model}
          </span>
        ))}
        {group.configs.length > 3 && (
          <span className="rounded-full border border-border bg-background/40 px-1.5 py-0.5 text-[10px] text-muted-foreground">
            +{group.configs.length - 3}
          </span>
        )}
        {group.configs.length === 0 && (
          <span className="text-[11px] text-muted-foreground">No models configured</span>
        )}
      </div>

      {mayManage && !isLocal && !isAuto && (
        <CompletionKeyPicker canonical={group.canonical} available={group.available} />
      )}

      {supportsReconciliation && mayManage && !isLocal && (
        <AdminKeyConnect canonical={group.canonical as "anthropic" | "openai"} />
      )}

      {mayManage && (
        <button
          type="button"
          onClick={handleRemoveProvider}
          disabled={removing}
          className="mt-auto self-start text-[11px] text-muted-foreground underline decoration-dotted underline-offset-2 hover:text-[color:var(--status-error)] disabled:cursor-not-allowed disabled:opacity-50"
        >
          {t("Remove provider", "Anbieter entfernen")}
        </button>
      )}
      {RemoveConfirmDialog}
    </div>
  );
}

// ---------- Completion key (BYOK) — used to make actual model requests -------
// Migrated to the unified credentials framework (§8): `<CredentialPicker>`
// reads/writes `Credential` rows of the core-owned `{canonical}_api_key`
// type (`oc8.credentials.core_types`) instead of a bare `useCreateSecret`
// call. Convention (not an enforced invariant -- nothing stops a tenant from
// ending up with two, e.g. via "Change key" -> "Create new" instead of
// re-selecting the existing one) is that a provider has at most one such
// credential, so the first result of useCredentials(credentialType) is
// treated as "the bound one" -- there is no separate binding to persist.
// This must agree with the backend's own "first" (`resolve_model_key` in
// `oc8.modelrouter.keys`, ordered by name to match `list_credentials`
// exactly) or the pill/picker here could show one credential as bound while
// completions silently keep using a different one. The "key set"/"key
// missing" pill on ProviderCard is
// driven by useModelProviders() (["model-providers"]), a separate query from
// useCreateCredential's own (["credentials"]) invalidation (Task 9) -- so a
// successful create/select here must explicitly invalidate
// ["model-providers"] too, or the pill won't flip until some unrelated
// refetch happens (the same staleness bug this file's old useCreateSecret
// mechanism already fixed once).
function CompletionKeyPicker({ canonical, available }: { canonical: string; available: boolean }) {
  const t = useT();
  // `available` arrives late: ProviderCard renders from useModels() before
  // useModelProviders() resolves, and defaults `available` to false until it
  // does. A useState(!available) initializer would latch that first false and
  // never let go, leaving a provider that HAS a key stuck showing the expanded
  // picker. So keep only the user's own intent in state and derive the rest --
  // that also makes the picker collapse by itself once a fresh key makes
  // `available` true.
  const [expanded, setExpanded] = useState(false);
  const open = expanded || !available;
  const credentialType = `${canonical}_api_key`;
  const { data: credentials = [] } = useCredentials(credentialType);
  const boundId = credentials[0]?.id ?? "";
  const qc = useQueryClient();

  const handleChange = (_credentialId: string) => {
    // The picker's own useCreateCredential already invalidated ["credentials"]
    // (Task 9); ["model-providers"] is a separate query and won't refetch on
    // its own.
    qc.invalidateQueries({ queryKey: ["model-providers"] });
    setExpanded(false);
  };

  if (available && !open) {
    return (
      <button
        type="button"
        onClick={() => setExpanded(true)}
        className="text-left text-[11px] text-muted-foreground underline decoration-dotted underline-offset-2 hover:text-primary"
      >
        {t("Change key", "Schlüssel ändern")}
      </button>
    );
  }

  return (
    <div className="space-y-2 rounded-md border border-dashed border-border bg-background/20 p-2.5">
      {!available ? (
        <p className="text-[11px] font-medium text-[color:var(--status-warning)]">
          {t(
            "This provider needs a completion key before its agents can run.",
            "Dieser Anbieter benötigt einen Completion-Schlüssel, bevor seine Agenten laufen können.",
          )}
        </p>
      ) : null}
      <CredentialPicker credentialType={credentialType} value={boundId} onChange={handleChange} />
      {available ? (
        <div className="flex items-center justify-end">
          <button
            type="button"
            onClick={() => setExpanded(false)}
            className="rounded-md border border-border bg-background/40 px-2 py-1 text-[11px] text-muted-foreground hover:text-foreground"
          >
            {t("Cancel", "Abbrechen")}
          </button>
        </div>
      ) : null}
    </div>
  );
}

// ---------- Admin key for cost reconciliation (Cost Center §Part C) ----------
// Reuses the existing generic /secrets store (useCreateSecret) -- there is no
// dedicated admin-key endpoint. This key is never used to make requests; the
// backend only reads it for the optional daily provider-cost fetch.
function AdminKeyConnect({ canonical }: { canonical: "anthropic" | "openai" }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [key, setKey] = useState("");
  const createSecret = useCreateSecret();
  const providerLabel = canonical === "anthropic" ? "Anthropic" : "OpenAI";

  const submit = async () => {
    if (!key.trim()) return;
    try {
      await createSecret.mutateAsync({
        name: `model/${canonical}/admin_key`,
        value: key.trim(),
        kind: "generic",
      });
      toast.success(
        t(`${providerLabel} admin key connected`, `${providerLabel}-Admin-Schlüssel verbunden`),
      );
      setKey("");
      setOpen(false);
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : t("Could not save key", "Schlüssel konnte nicht gespeichert werden"),
      );
    }
  };

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="text-left text-[11px] text-muted-foreground underline decoration-dotted underline-offset-2 hover:text-primary"
      >
        {t(
          "Connect admin key for cost validation (optional)",
          "Admin-Schlüssel für Kostenabgleich verbinden (optional)",
        )}
      </button>
    );
  }

  return (
    <div className="space-y-2 rounded-md border border-dashed border-border bg-background/20 p-2.5">
      <p className="text-[11px] text-muted-foreground">
        {t(
          `A separate, more-privileged key from your regular API key — generated in your ${providerLabel} console's Admin/Organization settings. Used only for a daily cost validation total, never to make requests.`,
          `Ein separater, stärker privilegierter Schlüssel als Ihr normaler API-Schlüssel — erzeugt in den Admin-/Organisationseinstellungen der ${providerLabel}-Konsole. Wird nur für einen täglichen Kostenabgleich verwendet, niemals für Anfragen.`,
        )}
      </p>
      <input
        type="password"
        value={key}
        onChange={(e) => setKey(e.target.value)}
        placeholder={t("Admin API key", "Admin-API-Schlüssel")}
        autoComplete="off"
        className="w-full rounded-md border border-border bg-background/40 px-2 py-1.5 text-xs font-mono outline-none focus:border-primary/50"
      />
      <div className="flex items-center justify-end gap-1.5">
        <button
          type="button"
          onClick={() => {
            setOpen(false);
            setKey("");
          }}
          className="rounded-md border border-border bg-background/40 px-2 py-1 text-[11px] text-muted-foreground hover:text-foreground"
        >
          {t("Cancel", "Abbrechen")}
        </button>
        <button
          type="button"
          disabled={createSecret.isPending || !key.trim()}
          onClick={submit}
          className="rounded-md bg-primary px-2 py-1 text-[11px] font-medium text-primary-foreground hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {t("Connect", "Verbinden")}
        </button>
      </div>
    </div>
  );
}

// Deterministic hue from a provider's canonical name, purely cosmetic.
function hashHue(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
  return h;
}

function Field({
  label,
  placeholder,
  mono,
  value,
  onChange,
}: {
  label: string;
  placeholder?: string;
  mono?: boolean;
  value?: string;
  onChange?: (value: string) => void;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-[10px] uppercase tracking-widest text-muted-foreground">
        {label}
      </span>
      <input
        type="text"
        placeholder={placeholder}
        value={value}
        onChange={onChange ? (e) => onChange(e.target.value) : undefined}
        className={cn(
          "w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50",
          mono && "font-mono text-xs",
        )}
      />
    </label>
  );
}

// ---------- Model create/edit dialog (real backend, §model registry) ----------
// Free-text model tag: the registry accepts arbitrary provider model strings
// (e.g. "llama3.1:8b", "claude-3-5-sonnet-20241022"), so the tag field is a
// plain input — never a fixed dropdown. Provider `auto` is a virtual router:
// no discover / model tag / credential — operators pick concrete tier models.
const AUTO_TIERS = ["fast", "balanced", "strong"] as const;

function ModelFormDialog({ editing, onClose }: { editing: ModelDTO | null; onClose: () => void }) {
  const t = useT();
  const { data: providers = [] } = useModelProviders();
  const { data: allModels = [] } = useModels();
  const createModel = useCreateModel();
  const updateModel = useUpdateModel();
  const discoverModels = useDiscoverModels();

  const [provider, setProvider] = useState(editing?.provider ?? "");
  const [modelTag, setModelTag] = useState(editing?.model ?? "");
  const [locality, setLocality] = useState<"cloud" | "local">(editing?.locality ?? "cloud");
  const [displayName, setDisplayName] = useState(editing?.displayName ?? "");
  const [usedByCopilot, setUsedByCopilot] = useState(editing?.usedByCopilot ?? false);
  const [supportsVision, setSupportsVision] = useState(editing?.supportsVision ?? false);
  const [maxTokens, setMaxTokens] = useState(
    editing?.maxTokens != null ? String(editing.maxTokens) : "",
  );
  const [effort, setEffort] = useState(editing?.effort ?? "");
  const [rawParams, setRawParams] = useState<RawParamPair[]>(() => extraToPairs(editing?.extra));
  // Create-mode only: discovery + multi-select, so adding several of a
  // provider's models doesn't mean typing each tag by hand. Manual entry
  // (the plain `modelTag` field above) stays the fallback for a provider
  // discovery can't reach or that returns nothing.
  const [discoveredModels, setDiscoveredModels] = useState<string[] | null>(null);
  const [selectedModels, setSelectedModels] = useState<Set<string>>(new Set());
  const [manualMode, setManualMode] = useState(false);
  const [tierFast, setTierFast] = useState(editing?.autoTiers?.fast ?? "");
  const [tierBalanced, setTierBalanced] = useState(editing?.autoTiers?.balanced ?? "");
  const [tierStrong, setTierStrong] = useState(editing?.autoTiers?.strong ?? "");
  const [autoShadowOnly, setAutoShadowOnly] = useState(editing?.autoShadowOnly ?? false);
  const [autoCascadeVerify, setAutoCascadeVerify] = useState(editing?.autoCascadeVerify ?? false);
  const [autoPreferenceRouter, setAutoPreferenceRouter] = useState(
    editing?.autoPreferenceRouter ?? false,
  );

  const effectiveProvider = provider || providers[0]?.canonical || "";
  const isAuto = isAutoProvider(effectiveProvider);
  const isPending = createModel.isPending || updateModel.isPending;
  const showPicker =
    !editing && !isAuto && discoveredModels !== null && discoveredModels.length > 0 && !manualMode;
  const tierTargets = allModels.filter((m) => !isAutoProvider(m.provider) && m.id !== editing?.id);

  const resetDiscovery = () => {
    setDiscoveredModels(null);
    setSelectedModels(new Set());
    setManualMode(false);
  };

  const fetchModels = async () => {
    if (!effectiveProvider || isAuto) return;
    try {
      const { models } = await discoverModels.mutateAsync({ provider: effectiveProvider });
      if (models.length === 0) {
        toast.error(t("Provider returned no models", "Anbieter hat keine Modelle zurückgegeben"));
        setManualMode(true);
        return;
      }
      setDiscoveredModels(models);
      setSelectedModels(new Set());
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : t("Could not fetch model list", "Modellliste konnte nicht geladen werden"),
      );
      setManualMode(true);
    }
  };

  const toggleModel = (id: string) => {
    setSelectedModels((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const buildAutoTiers = (): Record<string, string> | null => {
    const tiers: Record<string, string> = {};
    if (tierFast) tiers.fast = tierFast;
    if (tierBalanced) tiers.balanced = tierBalanced;
    if (tierStrong) tiers.strong = tierStrong;
    return Object.keys(tiers).length > 0 ? tiers : null;
  };

  const handleSubmit = async () => {
    if (!effectiveProvider) {
      toast.error(t("Provider is required", "Anbieter ist erforderlich"));
      return;
    }
    if (isAuto) {
      const autoTiers = buildAutoTiers();
      if (!autoTiers) {
        toast.error(
          t(
            "Pick at least one tier model (fast, balanced, or strong)",
            "Wähle mindestens ein Tier-Modell (fast, balanced oder strong)",
          ),
        );
        return;
      }
      const body: ModelWriteBody = {
        provider: AUTO_PROVIDER,
        model: "router",
        locality: "cloud",
        displayName: displayName.trim() || "Auto",
        usedByCopilot,
        autoTiers,
        autoShadowOnly,
        autoCascadeVerify,
        autoPreferenceRouter,
      };
      try {
        if (editing) {
          await updateModel.mutateAsync({ id: editing.id, ...body });
        } else {
          await createModel.mutateAsync(body);
        }
        toast.success(
          editing
            ? t("Auto router updated", "Auto-Router aktualisiert")
            : t("Auto router added", "Auto-Router hinzugefügt"),
        );
        onClose();
      } catch (err) {
        toast.error(
          err instanceof Error
            ? err.message
            : t("Something went wrong", "Etwas ist schiefgelaufen"),
        );
      }
      return;
    }
    if (showPicker) {
      if (selectedModels.size === 0) {
        toast.error(t("Pick at least one model", "Wähle mindestens ein Modell aus"));
        return;
      }
      try {
        await Promise.all(
          [...selectedModels].map((model) =>
            createModel.mutateAsync({ provider: effectiveProvider, model, locality }),
          ),
        );
        toast.success(
          t(
            `${selectedModels.size} model(s) added`,
            `${selectedModels.size} Modell(e) hinzugefügt`,
          ),
        );
        onClose();
      } catch (err) {
        toast.error(
          err instanceof Error
            ? err.message
            : t("Something went wrong", "Etwas ist schiefgelaufen"),
        );
      }
      return;
    }
    if (!modelTag.trim()) {
      toast.error(
        t("Provider and model tag are required", "Anbieter und Modell-Tag sind erforderlich"),
      );
      return;
    }
    const parsedMaxTokens = maxTokens.trim() ? Number(maxTokens.trim()) : undefined;
    if (
      parsedMaxTokens !== undefined &&
      (!Number.isFinite(parsedMaxTokens) || parsedMaxTokens < 1)
    ) {
      toast.error(t("Max tokens must be a positive number", "Max. Tokens muss positiv sein"));
      return;
    }
    const body = {
      provider: effectiveProvider,
      model: modelTag.trim(),
      locality,
      displayName: displayName.trim() || undefined,
      usedByCopilot,
      supportsVision,
      ...(editing ? { maxTokens: parsedMaxTokens ?? 0 } : {}),
      effort: effort.trim(),
      extra: pairsToExtra(rawParams) ?? {},
    };
    try {
      if (editing) {
        await updateModel.mutateAsync({ id: editing.id, ...body });
      } else {
        await createModel.mutateAsync(body);
      }
      toast.success(
        editing
          ? t(`${modelTag} updated`, `${modelTag} aktualisiert`)
          : t(`${modelTag} added`, `${modelTag} hinzugefügt`),
      );
      onClose();
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : t("Something went wrong", "Etwas ist schiefgelaufen"),
      );
    }
  };

  const tierValue = (tier: (typeof AUTO_TIERS)[number]) =>
    tier === "fast" ? tierFast : tier === "balanced" ? tierBalanced : tierStrong;
  const setTierValue = (tier: (typeof AUTO_TIERS)[number], value: string) => {
    if (tier === "fast") setTierFast(value);
    else if (tier === "balanced") setTierBalanced(value);
    else setTierStrong(value);
  };

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4" onClick={onClose}>
      <div
        className="w-full max-w-lg rounded-xl border border-border bg-panel p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="mb-4 flex items-start justify-between gap-3">
          <div>
            <div className="font-serif text-lg leading-tight">
              {editing
                ? isAuto
                  ? t("Edit Auto router", "Auto-Router bearbeiten")
                  : t("Edit model", "Modell bearbeiten")
                : t("New model", "Neues Modell")}
            </div>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {isAuto
                ? t(
                    "A virtual router that picks among your connected models by task complexity.",
                    "Ein virtueller Router, der unter deinen verbundenen Modellen nach Aufgabenkomplexität wählt.",
                  )
                : t(
                    "Registers a model config agents can be assigned to.",
                    "Registriert eine Modell-Konfiguration, die Agenten zugewiesen werden kann.",
                  )}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md p-1 text-muted-foreground hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        <div className="space-y-3">
          <label className="block">
            <span className="mb-1 block text-[10px] uppercase tracking-widest text-muted-foreground">
              {t("Provider", "Anbieter")}
            </span>
            <select
              value={effectiveProvider}
              onChange={(e) => {
                setProvider(e.target.value);
                if (!editing) resetDiscovery();
              }}
              className="w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
            >
              {providers.length === 0 && <option value="">{t("Loading…", "Lädt…")}</option>}
              {providers.map((p) => (
                <option key={p.canonical} value={p.canonical}>
                  {p.label || `${p.canonical} (${p.locality})`}
                </option>
              ))}
            </select>
          </label>

          {isAuto ? (
            <>
              <Field
                label={t("Display name (optional)", "Anzeigename (optional)")}
                placeholder="Auto"
                value={displayName}
                onChange={setDisplayName}
              />

              <div className="space-y-2 rounded-md border border-border bg-background/30 p-3">
                <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  {t("Tier models", "Tier-Modelle")}
                </div>
                <p className="text-[11px] text-muted-foreground">
                  {t(
                    "Map each complexity tier to an existing model config. At least one is required.",
                    "Ordne jedem Komplexitäts-Tier eine bestehende Modell-Konfiguration zu. Mindestens eine ist erforderlich.",
                  )}
                </p>
                {AUTO_TIERS.map((tier) => (
                  <label key={tier} className="block">
                    <span className="mb-1 block text-[10px] uppercase tracking-widest text-muted-foreground">
                      {tier}
                    </span>
                    <select
                      value={tierValue(tier)}
                      onChange={(e) => setTierValue(tier, e.target.value)}
                      className="w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
                    >
                      <option value="">{t("— none —", "— keines —")}</option>
                      {tierTargets.map((m) => (
                        <option key={m.id} value={m.id}>
                          {m.displayName || m.model} · {m.provider}
                        </option>
                      ))}
                    </select>
                  </label>
                ))}
                {tierTargets.length === 0 && (
                  <p className="text-[11px] text-[color:var(--status-warning)]">
                    {t(
                      "Add at least one concrete model first, then configure Auto tiers.",
                      "Lege zuerst mindestens ein konkretes Modell an, dann Auto-Tiers konfigurieren.",
                    )}
                  </p>
                )}
              </div>

              <div className="space-y-2">
                <label className="flex items-start gap-2 text-sm">
                  <input
                    type="checkbox"
                    className="mt-0.5"
                    checked={autoCascadeVerify}
                    onChange={(e) => setAutoCascadeVerify(e.target.checked)}
                  />
                  <span>
                    <span className="font-medium">{t("Cascade verify", "Cascade-Verify")}</span>
                    <span className="mt-0.5 block text-[11px] text-muted-foreground">
                      {t(
                        "After a cheap answer, escalate once if a self-check looks uncertain.",
                        "Nach einer günstigen Antwort einmal eskalieren, wenn der Self-Check unsicher wirkt.",
                      )}
                    </span>
                  </span>
                </label>
                <label className="flex items-start gap-2 text-sm">
                  <input
                    type="checkbox"
                    className="mt-0.5"
                    checked={autoPreferenceRouter}
                    onChange={(e) => setAutoPreferenceRouter(e.target.checked)}
                  />
                  <span>
                    <span className="font-medium">
                      {t("Preference router", "Preference-Router")}
                    </span>
                    <span className="mt-0.5 block text-[11px] text-muted-foreground">
                      {t(
                        "Blend the heuristic with learned preference examples from past runs.",
                        "Heuristik mit gelernten Preference-Beispielen aus früheren Läufen mischen.",
                      )}
                    </span>
                  </span>
                </label>
                <label className="flex items-start gap-2 text-sm">
                  <input
                    type="checkbox"
                    className="mt-0.5"
                    checked={autoShadowOnly}
                    onChange={(e) => setAutoShadowOnly(e.target.checked)}
                  />
                  <span>
                    <span className="font-medium">{t("Shadow only", "Nur Shadow")}</span>
                    <span className="mt-0.5 block text-[11px] text-muted-foreground">
                      {t(
                        "Log the complexity choice but always run balanced (or latched) until you trust the policy.",
                        "Komplexitätswahl loggen, aber immer balanced (oder gelatcht) laufen lassen, bis die Policy vertrauenswürdig ist.",
                      )}
                    </span>
                  </span>
                </label>
              </div>

              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={usedByCopilot}
                  onChange={(e) => setUsedByCopilot(e.target.checked)}
                />
                {t("Use this model for the Copilot", "Dieses Modell für den Copilot nutzen")}
              </label>
            </>
          ) : (
            <>
              {!editing && !manualMode && (
                <button
                  type="button"
                  onClick={fetchModels}
                  disabled={discoverModels.isPending || !effectiveProvider}
                  className="w-full rounded-md border border-border px-3 py-2 text-sm font-medium text-foreground transition hover:border-primary/40 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <RefreshCw className="mr-1.5 inline h-3.5 w-3.5" />
                  {discoverModels.isPending
                    ? t("Fetching models…", "Modelle werden geladen …")
                    : discoveredModels
                      ? t("Fetch again", "Erneut laden")
                      : t("Fetch available models", "Verfügbare Modelle laden")}
                </button>
              )}

              {showPicker ? (
                <div className="block">
                  <div className="mb-1 flex items-center justify-between">
                    <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
                      {t("Models", "Modelle")}
                    </span>
                    <button
                      type="button"
                      onClick={() => setManualMode(true)}
                      className="text-[11px] text-muted-foreground underline hover:text-foreground"
                    >
                      {t("Enter manually instead", "Stattdessen manuell eingeben")}
                    </button>
                  </div>
                  <div className="max-h-56 space-y-1 overflow-y-auto rounded-md border border-border bg-background/30 p-2">
                    {discoveredModels?.map((id) => (
                      <label
                        key={id}
                        className="flex items-center gap-2 rounded px-2 py-1.5 text-sm hover:bg-background/50"
                      >
                        <input
                          type="checkbox"
                          checked={selectedModels.has(id)}
                          onChange={() => toggleModel(id)}
                        />
                        <span className="font-mono text-xs">{id}</span>
                      </label>
                    ))}
                  </div>
                  <p className="mt-1 text-[11px] text-muted-foreground">
                    {t(`${selectedModels.size} selected`, `${selectedModels.size} ausgewählt`)}
                  </p>
                </div>
              ) : (
                <div className="block">
                  {!editing && manualMode && discoveredModels === null && (
                    <p className="mb-1 text-[11px] text-[color:var(--status-warning)]">
                      {t(
                        "Automatic discovery isn't available for this provider — enter the model tag manually.",
                        "Automatisches Abrufen ist für diesen Anbieter nicht verfügbar — Modell-Tag manuell eingeben.",
                      )}
                    </p>
                  )}
                  <Field
                    label={t("Model tag", "Modell-Tag")}
                    placeholder="llama3.1:8b / claude-3-5-sonnet-20241022"
                    mono
                    value={modelTag}
                    onChange={setModelTag}
                  />
                </div>
              )}

              <label className="block">
                <span className="mb-1 block text-[10px] uppercase tracking-widest text-muted-foreground">
                  {t("Locality", "Standort")}
                </span>
                <select
                  value={locality}
                  onChange={(e) => setLocality(e.target.value as "cloud" | "local")}
                  className="w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
                >
                  <option value="cloud">{t("Cloud", "Cloud")}</option>
                  <option value="local">{t("Local", "Lokal")}</option>
                </select>
              </label>

              {!showPicker && (
                <>
                  <Field
                    label={t("Display name (optional)", "Anzeigename (optional)")}
                    placeholder="e.g. Sales GPT"
                    value={displayName}
                    onChange={setDisplayName}
                  />

                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={usedByCopilot}
                      onChange={(e) => setUsedByCopilot(e.target.checked)}
                    />
                    {t("Use this model for the Copilot", "Dieses Modell für den Copilot nutzen")}
                  </label>

                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={supportsVision}
                      onChange={(e) => setSupportsVision(e.target.checked)}
                    />
                    {t(
                      "This model can see images (vision)",
                      "Dieses Modell kann Bilder verarbeiten (Vision)",
                    )}
                  </label>

                  {editing && (
                    <label className="block">
                      <span className="mb-1 block text-[10px] uppercase tracking-widest text-muted-foreground">
                        {t("Max output tokens (optional)", "Max. Output-Tokens (optional)")}
                      </span>
                      <input
                        type="number"
                        min={1}
                        placeholder="1536"
                        value={maxTokens}
                        onChange={(e) => setMaxTokens(e.target.value)}
                        className="w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
                      />
                      <p className="mt-1 text-[11px] text-muted-foreground">
                        {t(
                          "How much an agent on this model may write per turn, before oc8 cuts it off. Leave blank for the framework default (1536). Raise this for reasoning-heavy models on large tasks — hidden reasoning tokens count against this budget too.",
                          "Wie viel ein Agent auf diesem Modell pro Zug schreiben darf, bevor oc8 abschneidet. Leer lassen für den Standard (1536). Bei reasoning-lastigen Modellen und großen Aufgaben höher setzen — auch unsichtbare Reasoning-Tokens zählen gegen dieses Budget.",
                        )}
                      </p>
                    </label>
                  )}

                  <label className="block">
                    <span className="mb-1 block text-[10px] uppercase tracking-widest text-muted-foreground">
                      {t("Effort (optional)", "Effort (optional)")}
                    </span>
                    <input
                      type="text"
                      placeholder="e.g. high"
                      value={effort}
                      onChange={(e) => setEffort(e.target.value)}
                      className="w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
                    />
                    <p className="mt-1 text-[11px] text-muted-foreground">
                      {t(
                        "Reasoning effort forwarded as-is to providers that support it (currently Anthropic). Leave blank to omit. Not validated here — the provider decides which values it accepts.",
                        "Reasoning-Aufwand, unverändert an Anbieter weitergereicht, die dies unterstützen (aktuell Anthropic). Leer lassen, um es wegzulassen. Wird hier nicht validiert — der Anbieter entscheidet, welche Werte er akzeptiert.",
                      )}
                    </p>
                  </label>

                  {supportsRawParams(effectiveProvider) && (
                    <RawParamsEditor pairs={rawParams} onChange={setRawParams} />
                  )}
                </>
              )}
            </>
          )}
        </div>

        <footer className="mt-5 flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-border bg-background/40 px-3 py-2 text-sm text-muted-foreground hover:text-foreground"
          >
            {t("Cancel", "Abbrechen")}
          </button>
          <button
            type="button"
            disabled={isPending || (showPicker && selectedModels.size === 0)}
            onClick={handleSubmit}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:brightness-110 glow-teal disabled:cursor-not-allowed disabled:opacity-60"
          >
            <CheckCircle2 className="h-4 w-4" />
            {editing
              ? t("Save changes", "Änderungen speichern")
              : showPicker
                ? t(
                    `Add ${selectedModels.size} model(s)`,
                    `${selectedModels.size} Modell(e) hinzufügen`,
                  )
                : isAuto
                  ? t("Add Auto router", "Auto-Router hinzufügen")
                  : t("Add model", "Modell hinzufügen")}
          </button>
        </footer>
      </div>
    </div>
  );
}

// ---------- Add-provider wizard (real creation via useCreateModel) ----------

type WizardStep = 1 | 2 | 3;

// `SUBSCRIPTION_PROVIDER` (defined near the top of this file, alongside
// `SubscriptionRiskBadge`) is why step 2 below swaps in
// `ChatGptSubscriptionPicker`, and why step 1's tile must NOT show the
// generic key badge: its `available` flag is unconditionally true for this
// provider server-side (there is no tenant-wide key to check) and would
// therefore claim "key set" for a tenant that has never signed in.
export function AddProviderWizard({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  /** Called with the newly created model right before onClose -- lets a
   * caller (e.g. onboarding's AgentModelStep) select it immediately instead
   * of waiting for the models list to refetch. */
  onCreated?: (model: ModelDTO) => void;
}) {
  const t = useT();
  const { data: providers = [] } = useModelProviders();
  const createModel = useCreateModel();
  const discoverModels = useDiscoverModels();

  const [step, setStep] = useState<WizardStep>(1);
  const [canonical, setCanonical] = useState<string | null>(null);
  const [modelTag, setModelTag] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [locality, setLocality] = useState<"cloud" | "local">("cloud");
  const [credentialId, setCredentialId] = useState("");
  const [discoveredModels, setDiscoveredModels] = useState<string[] | null>(null);

  const provider = providers.find((p) => p.canonical === canonical) ?? null;

  const selectProvider = (p: ModelProviderDTO) => {
    setCanonical(p.canonical);
    setLocality(p.locality === "local" ? "local" : "cloud");
    setCredentialId("");
    setDiscoveredModels(null);
  };

  const fetchModels = async () => {
    if (!provider) return;
    try {
      const { models } = await discoverModels.mutateAsync({
        provider: provider.canonical,
        credentialId: credentialId || undefined,
      });
      setDiscoveredModels(models);
      if (models.length > 0) {
        setModelTag(models[0]);
      } else {
        toast.error(t("Provider returned no models", "Anbieter hat keine Modelle zurückgegeben"));
      }
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : t("Could not fetch model list", "Modellliste konnte nicht geladen werden"),
      );
    }
  };

  const handleCreate = async () => {
    if (!provider || !modelTag.trim()) return;
    try {
      const model = await createModel.mutateAsync({
        provider: provider.canonical,
        model: modelTag.trim(),
        locality,
        displayName: displayName.trim() || undefined,
        credentialId: credentialId || undefined,
      });
      toast.success(t(`${modelTag} added`, `${modelTag} hinzugefügt`), {
        description: t(
          "Models are now available for agents.",
          "Modelle sind jetzt für Agenten verfügbar.",
        ),
      });
      onCreated?.(model);
      onClose();
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : t("Something went wrong", "Etwas ist schiefgelaufen"),
      );
    }
  };

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4" onClick={onClose}>
      <div
        className="w-full max-w-2xl rounded-xl border border-border bg-panel shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-start justify-between gap-3 border-b border-border px-5 py-4">
          <div className="flex items-start gap-3">
            <div className="grid h-10 w-10 place-items-center rounded-md bg-primary/15 text-primary">
              <Sparkles className="h-4 w-4" />
            </div>
            <div>
              <div className="font-serif text-lg leading-tight">Add model provider</div>
              <p className="mt-0.5 text-xs text-muted-foreground">
                Step {step} of 3 —{" "}
                {step === 1 ? "pick a provider" : step === 2 ? "model details" : "confirm"}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md p-1 text-muted-foreground hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        {/* Stepper */}
        <div className="flex items-center gap-2 px-5 pt-4">
          {[1, 2, 3].map((n) => (
            <div key={n} className="flex flex-1 items-center gap-2">
              <div
                className={cn(
                  "grid h-6 w-6 place-items-center rounded-full border text-[11px] font-semibold",
                  step === n
                    ? "border-primary bg-primary/20 text-primary"
                    : step > n
                      ? "border-primary/40 bg-primary/10 text-primary"
                      : "border-border bg-background/40 text-muted-foreground",
                )}
              >
                {step > n ? <CheckCircle2 className="h-3.5 w-3.5" /> : n}
              </div>
              {n < 3 && (
                <div className={cn("h-px flex-1", step > n ? "bg-primary/40" : "bg-border")} />
              )}
            </div>
          ))}
        </div>

        <div className="max-h-[60vh] overflow-y-auto p-5">
          {step === 1 && (
            <div className="space-y-2">
              {providers.length === 0 && (
                <div className="rounded-md border border-dashed border-border bg-background/30 p-6 text-center text-sm text-muted-foreground">
                  {t("Loading providers…", "Anbieter werden geladen…")}
                </div>
              )}
              {providers
                .filter((p) => p.canonical !== AUTO_PROVIDER)
                .map((p) => {
                  const active = canonical === p.canonical;
                  const isLocal = p.locality === "local";
                  return (
                    <button
                      key={p.canonical}
                      type="button"
                      onClick={() => selectProvider(p)}
                      className={cn(
                        "flex w-full items-start gap-3 rounded-lg border p-3 text-left transition",
                        active
                          ? "border-primary/60 bg-primary/10"
                          : "border-border bg-background/30 hover:bg-background/50",
                      )}
                    >
                      <div
                        className="grid h-10 w-10 shrink-0 place-items-center rounded-md font-serif text-black"
                        style={{ background: `oklch(0.75 0.14 ${hashHue(p.canonical)})` }}
                      >
                        {p.canonical[0]?.toUpperCase()}
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="font-medium">{p.canonical}</span>
                          <span className="rounded-full border border-border bg-background/40 px-1.5 py-0.5 text-[10px] text-muted-foreground">
                            {isLocal ? "local" : "cloud"}
                          </span>
                          {!isLocal && p.canonical !== SUBSCRIPTION_PROVIDER && (
                            <span
                              className={cn(
                                "rounded-full border px-1.5 py-0.5 text-[10px]",
                                p.available
                                  ? "border-[color:var(--status-running)]/40 bg-[color:var(--status-running)]/10 text-[color:var(--status-running)]"
                                  : "border-[color:var(--status-warning)]/40 bg-[color:var(--status-warning)]/10 text-[color:var(--status-warning)]",
                              )}
                            >
                              {p.available ? "key set" : "key missing"}
                            </span>
                          )}
                          {p.canonical === SUBSCRIPTION_PROVIDER && <SubscriptionRiskBadge />}
                        </div>
                      </div>
                      {active && <CheckCircle2 className="mt-1 h-4 w-4 text-primary" />}
                    </button>
                  );
                })}
            </div>
          )}

          {step === 2 && provider && (
            <div className="space-y-3">
              <div className="flex items-start gap-3 rounded-lg border border-border bg-background/30 p-3">
                <div
                  className="grid h-10 w-10 shrink-0 place-items-center rounded-md font-serif text-black"
                  style={{ background: `oklch(0.75 0.14 ${hashHue(provider.canonical)})` }}
                >
                  {provider.canonical[0]?.toUpperCase()}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="font-medium">{provider.canonical}</div>
                </div>
              </div>

              {provider.locality !== "local" && provider.canonical === SUBSCRIPTION_PROVIDER && (
                <label className="block">
                  <span className="mb-1 block text-[10px] uppercase tracking-widest text-muted-foreground">
                    {t("ChatGPT account", "ChatGPT-Konto")}
                  </span>
                  <ChatGptSubscriptionPicker value={credentialId} onChange={setCredentialId} />
                  {!credentialId && (
                    <p className="mt-1 text-[11px] text-[color:var(--status-warning)]">
                      {/* Unlike an API-key provider, there is no tenant-wide
                          fallback for this one: the model router only reaches
                          the subscription bridge through an explicitly bound
                          credential, so a model added without one cannot run
                          at all. */}
                      {t(
                        "Sign in first — a model on this provider needs its own ChatGPT account and has no shared-key fallback.",
                        "Zuerst anmelden — ein Modell dieses Anbieters braucht ein eigenes ChatGPT-Konto; einen gemeinsamen Schlüssel als Rückfallebene gibt es hier nicht.",
                      )}
                    </p>
                  )}
                </label>
              )}
              {provider.locality !== "local" && provider.canonical !== SUBSCRIPTION_PROVIDER && (
                <label className="block">
                  <span className="mb-1 block text-[10px] uppercase tracking-widest text-muted-foreground">
                    {t("Credential", "Anmeldedaten")}
                  </span>
                  <CredentialPicker
                    credentialType={`${provider.canonical}_api_key`}
                    value={credentialId}
                    onChange={setCredentialId}
                  />
                  {!credentialId && (
                    <p className="mt-1 text-[11px] text-[color:var(--status-warning)]">
                      {t(
                        "No credential selected yet — pick or create one, or the platform's shared key is used if available.",
                        "Noch keine Anmeldedaten ausgewählt — wähle oder erstelle welche, sonst wird der gemeinsame Plattform-Schlüssel verwendet, falls vorhanden.",
                      )}
                    </p>
                  )}
                </label>
              )}

              <button
                type="button"
                onClick={fetchModels}
                disabled={discoverModels.isPending}
                className="w-full rounded-md border border-border px-3 py-2 text-sm font-medium text-foreground transition hover:border-primary/40 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {discoverModels.isPending
                  ? t("Fetching models…", "Modelle werden geladen …")
                  : t("Fetch available models", "Verfügbare Modelle laden")}
              </button>

              <label className="block">
                <span className="mb-1 block text-[10px] uppercase tracking-widest text-muted-foreground">
                  {t("Model", "Modell")}
                </span>
                {discoveredModels && discoveredModels.length > 0 ? (
                  <select
                    value={modelTag}
                    onChange={(e) => setModelTag(e.target.value)}
                    className="w-full rounded-md border border-border bg-background/40 px-3 py-2 font-mono text-xs outline-none focus:border-primary/50"
                  >
                    {discoveredModels.map((id) => (
                      <option key={id} value={id}>
                        {id}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    value={modelTag}
                    onChange={(e) => setModelTag(e.target.value)}
                    placeholder="llama3.1:8b"
                    className="w-full rounded-md border border-border bg-background/40 px-3 py-2 font-mono text-xs outline-none focus:border-primary/50"
                  />
                )}
              </label>

              <label className="block">
                <span className="mb-1 block text-[10px] uppercase tracking-widest text-muted-foreground">
                  {t("Locality", "Standort")}
                </span>
                <select
                  value={locality}
                  onChange={(e) => setLocality(e.target.value as "cloud" | "local")}
                  className="w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
                >
                  <option value="cloud">{t("Cloud", "Cloud")}</option>
                  <option value="local">{t("Local", "Lokal")}</option>
                </select>
              </label>

              <Field
                label={t("Display name (optional)", "Anzeigename (optional)")}
                placeholder="e.g. Sales GPT"
                value={displayName}
                onChange={setDisplayName}
              />
            </div>
          )}

          {step === 3 && provider && (
            <div className="space-y-3">
              <div className="rounded-lg border border-border bg-background/30 p-4 text-sm">
                <div className="flex items-center justify-between py-1">
                  <span className="text-muted-foreground">{t("Provider", "Anbieter")}</span>
                  <span className="font-medium">{provider.canonical}</span>
                </div>
                <div className="flex items-center justify-between py-1">
                  <span className="text-muted-foreground">{t("Model tag", "Modell-Tag")}</span>
                  <span className="font-mono text-xs">{modelTag || "—"}</span>
                </div>
                <div className="flex items-center justify-between py-1">
                  <span className="text-muted-foreground">{t("Locality", "Standort")}</span>
                  <span className="font-medium">{locality}</span>
                </div>
                <div className="flex items-center justify-between py-1">
                  <span className="text-muted-foreground">{t("Display name", "Anzeigename")}</span>
                  <span className="font-medium">{displayName || "—"}</span>
                </div>
              </div>
            </div>
          )}
        </div>

        <footer className="flex items-center justify-between gap-2 border-t border-border px-5 py-3">
          <button
            type="button"
            onClick={() => {
              if (step === 1) onClose();
              else setStep((s) => (s - 1) as WizardStep);
            }}
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background/40 px-3 py-2 text-sm text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            {step === 1 ? "Cancel" : "Back"}
          </button>
          {step < 3 ? (
            <button
              type="button"
              disabled={
                step === 1
                  ? !canonical
                  : // A ChatGPT-subscription model is unrunnable without an
                    // explicitly bound credential: the router's tenant-wide
                    // fallback only ever looks for `{canonical}_api_key`, a
                    // type this provider never has. Every other provider can
                    // still fall back to the shared platform key, so only
                    // this one blocks.
                    !modelTag.trim() ||
                    (provider?.canonical === SUBSCRIPTION_PROVIDER && !credentialId)
              }
              onClick={() => setStep((s) => (s + 1) as WizardStep)}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:brightness-110 glow-teal disabled:cursor-not-allowed disabled:opacity-40"
            >
              Continue <ArrowRight className="h-3.5 w-3.5" />
            </button>
          ) : (
            <button
              type="button"
              disabled={createModel.isPending}
              onClick={handleCreate}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:brightness-110 glow-teal disabled:cursor-not-allowed disabled:opacity-60"
            >
              <CheckCircle2 className="h-4 w-4" /> {t("Add model", "Modell hinzufügen")}
            </button>
          )}
        </footer>
      </div>
    </div>
  );
}
interface PriceDraft {
  priceIn: string;
  priceOut: string;
}

interface NewPriceDraft {
  provider: string;
  modelPattern: string;
  priceIn: string;
  priceOut: string;
}

const EMPTY_NEW_PRICE: NewPriceDraft = {
  provider: "",
  modelPattern: "",
  priceIn: "",
  priceOut: "",
};

export function ModelPricingPanel() {
  const t = useT();
  const may = useMay();
  const mayManage = may("model:manage");
  const { data: prices = [] } = useModelPrices();
  // Shares the ["models"] query cache with ModelsPage's own useModels() call
  // (same key, react-query dedupes) -- this panel is exported standalone
  // (used elsewhere on the page) so it fetches for itself rather than
  // threading the list through as a prop.
  const { data: models = [] } = useModels();
  const createPrice = useCreateModelPrice();
  const deactivatePrice = useDeactivateModelPrice();
  const { confirm, ConfirmDialog } = useConfirm();

  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<PriceDraft>({ priceIn: "", priceOut: "" });
  const [adding, setAdding] = useState(false);
  const [newPrice, setNewPrice] = useState<NewPriceDraft>(EMPTY_NEW_PRICE);
  // Dropdown-over-a-registered-model is the default (picking one sets both
  // modelPattern and provider together, so they can never mismatch); manual
  // entry stays available for a pricing rule that doesn't correspond to any
  // currently-registered ModelConfig yet (e.g. pricing set up ahead of
  // actually connecting the model).
  const [manualEntry, setManualEntry] = useState(false);

  const selectRegisteredModel = (modelTag: string) => {
    const match = models.find((m) => m.model === modelTag);
    setNewPrice((n) => ({
      ...n,
      modelPattern: modelTag,
      provider: match?.provider ?? n.provider,
    }));
  };

  const startEdit = (p: ModelPrice) => {
    setEditingId(p.id);
    setDraft({ priceIn: String(p.priceInUsdPer1M), priceOut: String(p.priceOutUsdPer1M) });
  };

  const cancelEdit = () => setEditingId(null);

  const saveEdit = async (p: ModelPrice) => {
    const priceIn = parseFloat(draft.priceIn);
    const priceOut = parseFloat(draft.priceOut);
    if (!Number.isFinite(priceIn) || priceIn < 0 || !Number.isFinite(priceOut) || priceOut < 0) {
      toast.error(
        t("Enter valid, non-negative prices.", "Bitte gültige, nicht-negative Preise eingeben."),
      );
      return;
    }
    try {
      // A new version row, not an in-place update -- see the hook's docstring.
      await createPrice.mutateAsync({
        provider: p.provider,
        modelPattern: p.modelPattern,
        priceInUsdPer1M: priceIn,
        priceOutUsdPer1M: priceOut,
      });
      setEditingId(null);
      toast.success(t("Price updated", "Preis aktualisiert"));
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : t("Could not save price", "Preis konnte nicht gespeichert werden"),
      );
    }
  };

  const removePrice = async (p: ModelPrice) => {
    const ok = await confirm({
      title: t("Remove pricing?", "Preis entfernen?"),
      description: t(
        `Remove pricing for ${p.modelPattern}? This cannot be undone.`,
        `Preis für ${p.modelPattern} entfernen? Dies kann nicht rückgängig gemacht werden.`,
      ),
      confirmLabel: t("Remove", "Entfernen"),
      cancelLabel: t("Cancel", "Abbrechen"),
    });
    if (!ok) return;
    try {
      await deactivatePrice.mutateAsync(p.id);
      toast.success(t(`${p.modelPattern} deactivated`, `${p.modelPattern} deaktiviert`));
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : t("Could not remove price", "Preis konnte nicht entfernt werden"),
      );
    }
  };

  const submitNewPrice = async () => {
    const priceIn = parseFloat(newPrice.priceIn);
    const priceOut = parseFloat(newPrice.priceOut);
    if (
      !newPrice.provider.trim() ||
      !newPrice.modelPattern.trim() ||
      !Number.isFinite(priceIn) ||
      priceIn < 0 ||
      !Number.isFinite(priceOut) ||
      priceOut < 0
    ) {
      toast.error(
        t(
          "Fill in provider, model pattern, and valid prices.",
          "Anbieter, Modell-Muster und gültige Preise eingeben.",
        ),
      );
      return;
    }
    try {
      await createPrice.mutateAsync({
        provider: newPrice.provider.trim(),
        modelPattern: newPrice.modelPattern.trim(),
        priceInUsdPer1M: priceIn,
        priceOutUsdPer1M: priceOut,
      });
      setAdding(false);
      setNewPrice(EMPTY_NEW_PRICE);
      toast.success(t("Model price added", "Modellpreis hinzugefügt"));
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : t("Could not add price", "Preis konnte nicht hinzugefügt werden"),
      );
    }
  };

  return (
    <Panel className="overflow-hidden">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-5 py-3">
        <div>
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
            {t("Model pricing", "Modellpreise")}
          </div>
          <div className="font-serif text-base">
            {t("USD per 1M tokens — editable", "USD pro 1 Mio. Token — bearbeitbar")}
          </div>
        </div>
        {mayManage && (
          <button
            type="button"
            onClick={() => {
              setAdding(true);
              setNewPrice(EMPTY_NEW_PRICE);
              setManualEntry(false);
            }}
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background/40 px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground"
          >
            <Plus className="h-3.5 w-3.5" /> {t("Add model", "Modell hinzufügen")}
          </button>
        )}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs uppercase tracking-wider text-muted-foreground">
              <th className="px-5 py-3 font-medium">{t("Model", "Modell")}</th>
              <th className="px-5 py-3 font-medium">{t("Provider", "Anbieter")}</th>
              <th className="px-5 py-3 font-medium text-right">
                {t("Input / 1M", "Input / 1 Mio.")}
              </th>
              <th className="px-5 py-3 font-medium text-right">
                {t("Output / 1M", "Output / 1 Mio.")}
              </th>
              <th className="px-5 py-3 font-medium text-right">
                {t("Est. cost / 1k req", "Ø / 1k Anfr.")}
              </th>
              <th className="px-5 py-3 font-medium">{t("Used by", "Verwendet von")}</th>
              {mayManage && <th className="px-5 py-3 font-medium text-right" />}
            </tr>
          </thead>
          <tbody>
            {adding && (
              <tr className="border-b border-border/60 bg-background/20">
                <td className="px-5 py-3">
                  {manualEntry || models.length === 0 ? (
                    <input
                      type="text"
                      placeholder={t("model pattern", "Modell-Muster")}
                      value={newPrice.modelPattern}
                      onChange={(e) => setNewPrice((n) => ({ ...n, modelPattern: e.target.value }))}
                      className="w-32 rounded-md border border-border bg-background/40 px-2 py-1 text-xs focus:border-primary focus:outline-none"
                    />
                  ) : (
                    <select
                      value={newPrice.modelPattern}
                      onChange={(e) => selectRegisteredModel(e.target.value)}
                      className="w-40 rounded-md border border-border bg-background/40 px-2 py-1 text-xs focus:border-primary focus:outline-none"
                    >
                      <option value="">{t("Select a model…", "Modell auswählen…")}</option>
                      {models.map((m) => (
                        <option key={m.id} value={m.model}>
                          {m.name} ({m.model})
                        </option>
                      ))}
                    </select>
                  )}
                  {models.length > 0 && (
                    <button
                      type="button"
                      onClick={() => setManualEntry((v) => !v)}
                      className="mt-1 block text-[10px] text-muted-foreground underline decoration-dotted underline-offset-2 hover:text-foreground"
                    >
                      {manualEntry
                        ? t("Choose from available models", "Aus verfügbaren Modellen wählen")
                        : t("Enter manually", "Manuell eingeben")}
                    </button>
                  )}
                </td>
                <td className="px-5 py-3">
                  <input
                    type="text"
                    readOnly={!manualEntry && models.length > 0}
                    placeholder={t("provider", "Anbieter")}
                    value={newPrice.provider}
                    onChange={(e) => setNewPrice((n) => ({ ...n, provider: e.target.value }))}
                    title={
                      !manualEntry && models.length > 0
                        ? t(
                            "Set automatically from the selected model",
                            "Wird automatisch anhand des gewählten Modells gesetzt",
                          )
                        : undefined
                    }
                    className={cn(
                      "w-24 rounded-md border border-border bg-background/40 px-2 py-1 text-xs focus:border-primary focus:outline-none",
                      !manualEntry && models.length > 0 && "cursor-not-allowed opacity-70",
                    )}
                  />
                </td>
                <td className="px-5 py-3">
                  <div className="flex items-center justify-end gap-1">
                    <span className="text-muted-foreground">$</span>
                    <input
                      type="number"
                      step="0.05"
                      min="0"
                      value={newPrice.priceIn}
                      onChange={(e) => setNewPrice((n) => ({ ...n, priceIn: e.target.value }))}
                      className="w-20 rounded-md border border-border bg-background/40 px-2 py-1 text-right font-mono text-xs focus:border-primary focus:outline-none"
                    />
                  </div>
                </td>
                <td className="px-5 py-3">
                  <div className="flex items-center justify-end gap-1">
                    <span className="text-muted-foreground">$</span>
                    <input
                      type="number"
                      step="0.05"
                      min="0"
                      value={newPrice.priceOut}
                      onChange={(e) => setNewPrice((n) => ({ ...n, priceOut: e.target.value }))}
                      className="w-20 rounded-md border border-border bg-background/40 px-2 py-1 text-right font-mono text-xs focus:border-primary focus:outline-none"
                    />
                  </div>
                </td>
                <td className="px-5 py-3" />
                <td className="px-5 py-3" />
                <td className="px-5 py-3 text-right">
                  <div className="flex items-center justify-end gap-1.5">
                    <button
                      type="button"
                      disabled={createPrice.isPending}
                      onClick={submitNewPrice}
                      className="inline-flex items-center gap-1 rounded-md bg-primary px-2 py-1 text-xs font-medium text-primary-foreground hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      <CheckCircle2 className="h-3 w-3" /> {t("Save", "Speichern")}
                    </button>
                    <button
                      type="button"
                      onClick={() => setAdding(false)}
                      className="inline-flex items-center gap-1 rounded-md border border-border bg-background/40 px-2 py-1 text-xs text-muted-foreground hover:text-foreground"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </div>
                </td>
              </tr>
            )}
            {prices.map((p) => {
              const usingAgents = agents.filter((a) => a.llm === p.modelPattern);
              const per1kUSD = (1500 * p.priceInUsdPer1M + 500 * p.priceOutUsdPer1M) / 1000;
              const isEditing = editingId === p.id;
              return (
                <tr key={p.id} className="border-b border-border/60 last:border-none">
                  <td className="px-5 py-3">
                    <div className="flex items-center gap-2 font-medium">
                      <Cpu className="h-3.5 w-3.5 text-primary" />
                      {p.modelPattern}
                    </div>
                  </td>
                  <td className="px-5 py-3">
                    <span className="rounded-full border border-border bg-background/40 px-2 py-0.5 text-xs">
                      {p.provider}
                    </span>
                  </td>
                  <td className="px-5 py-3">
                    <div className="flex items-center justify-end gap-1">
                      <span className="text-muted-foreground">$</span>
                      {isEditing ? (
                        <input
                          type="number"
                          step="0.05"
                          min="0"
                          value={draft.priceIn}
                          onChange={(e) => setDraft((d) => ({ ...d, priceIn: e.target.value }))}
                          className="w-20 rounded-md border border-border bg-background/40 px-2 py-1 text-right font-mono text-xs focus:border-primary focus:outline-none"
                        />
                      ) : (
                        <span className="font-mono text-xs">{p.priceInUsdPer1M.toFixed(2)}</span>
                      )}
                    </div>
                  </td>
                  <td className="px-5 py-3">
                    <div className="flex items-center justify-end gap-1">
                      <span className="text-muted-foreground">$</span>
                      {isEditing ? (
                        <input
                          type="number"
                          step="0.05"
                          min="0"
                          value={draft.priceOut}
                          onChange={(e) => setDraft((d) => ({ ...d, priceOut: e.target.value }))}
                          className="w-20 rounded-md border border-border bg-background/40 px-2 py-1 text-right font-mono text-xs focus:border-primary focus:outline-none"
                        />
                      ) : (
                        <span className="font-mono text-xs">{p.priceOutUsdPer1M.toFixed(2)}</span>
                      )}
                    </div>
                  </td>
                  <td className="px-5 py-3 text-right font-mono text-xs text-muted-foreground">
                    ${per1kUSD.toFixed(2)}
                  </td>
                  <td className="px-5 py-3">
                    <div className="flex flex-wrap gap-1">
                      {usingAgents.map((a) => (
                        <span
                          key={a.id}
                          className="inline-flex items-center gap-1 rounded-full border border-border bg-background/30 px-2 py-0.5 text-[10px]"
                        >
                          <span
                            className="h-1.5 w-1.5 rounded-full"
                            style={{ background: a.avatarColor }}
                          />
                          {a.name}
                        </span>
                      ))}
                      {usingAgents.length === 0 && (
                        <span className="text-[11px] text-muted-foreground">
                          {t("— no agent", "— kein Agent")}
                        </span>
                      )}
                    </div>
                  </td>
                  {mayManage && (
                    <td className="px-5 py-3 text-right">
                      {isEditing ? (
                        <div className="flex items-center justify-end gap-1.5">
                          <button
                            type="button"
                            disabled={createPrice.isPending}
                            onClick={() => saveEdit(p)}
                            className="inline-flex items-center gap-1 rounded-md bg-primary px-2 py-1 text-xs font-medium text-primary-foreground hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-60"
                          >
                            <CheckCircle2 className="h-3 w-3" /> {t("Save", "Speichern")}
                          </button>
                          <button
                            type="button"
                            onClick={cancelEdit}
                            className="inline-flex items-center gap-1 rounded-md border border-border bg-background/40 px-2 py-1 text-xs text-muted-foreground hover:text-foreground"
                          >
                            <X className="h-3 w-3" />
                          </button>
                        </div>
                      ) : (
                        <div className="flex items-center justify-end gap-1.5">
                          <button
                            type="button"
                            onClick={() => startEdit(p)}
                            className="inline-flex items-center gap-1 rounded-md border border-border bg-background/40 px-2 py-1 text-xs text-muted-foreground hover:text-foreground"
                          >
                            <Pencil className="h-3 w-3" /> {t("Edit", "Bearbeiten")}
                          </button>
                          <button
                            type="button"
                            onClick={() => removePrice(p)}
                            className="inline-flex items-center gap-1 rounded-md border border-border bg-background/40 px-2 py-1 text-xs text-muted-foreground hover:text-[color:var(--status-error)]"
                          >
                            <Trash2 className="h-3 w-3" /> {t("Remove", "Entfernen")}
                          </button>
                        </div>
                      )}
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="border-t border-border px-5 py-3 text-[11px] text-muted-foreground">
        {t(
          "Estimate assumes ~1,500 input + 500 output tokens per request. Cost totals per agent and department use these prices.",
          "Schätzung geht von ~1.500 Input + 500 Output Token pro Anfrage aus. Kostenauswertungen pro Agent und Abteilung verwenden diese Preise.",
        )}
      </div>
      {ConfirmDialog}
    </Panel>
  );
}
