import { useState } from "react";
import { toast } from "sonner";
import { useT } from "@/lib/i18n";
import { useCreateModel, type ModelDTO, type ModelProviderDTO } from "@/lib/hooks";
import { cn } from "@/lib/utils";
import {
  AutoRouterBadge,
  AUTO_PROVIDER,
  isAutoProvider,
  modelAssignmentLabel,
} from "@/routes/models";

export function ModelPicker({
  models,
  providers,
  selectedId,
  onSelect,
  onModelCreated,
}: {
  models: ModelDTO[];
  providers: ModelProviderDTO[];
  selectedId: string;
  onSelect: (modelId: string) => void;
  onModelCreated?: (model: ModelDTO) => void;
}) {
  if (models.length === 0) {
    return (
      <ConnectModelInline
        providers={providers}
        onCreated={(model) => {
          onSelect(model.id);
          onModelCreated?.(model);
        }}
      />
    );
  }

  return (
    <div className="grid gap-2">
      {models.map((m) => (
        <label
          key={m.id}
          className={cn(
            "flex cursor-pointer items-center justify-between gap-3 rounded-lg border p-3.5 text-sm transition",
            selectedId === m.id
              ? "border-primary bg-primary/10 glow-teal"
              : "border-border bg-background/30 hover:border-primary/40",
          )}
        >
          <div className="flex items-center gap-3">
            <input
              type="radio"
              name="llm"
              className="accent-[color:var(--primary)]"
              checked={selectedId === m.id}
              onChange={() => onSelect(m.id)}
            />
            <div>
              <div className="flex items-center gap-2 font-medium">
                {m.name}
                {isAutoProvider(m.provider) && <AutoRouterBadge />}
              </div>
              <div className="text-xs text-muted-foreground">
                {isAutoProvider(m.provider)
                  ? "Auto · complexity router"
                  : `${m.provider} · ${m.latency} · cost ${m.costTier}`}
              </div>
            </div>
          </div>
          <span className="hidden max-w-[240px] text-right text-xs text-muted-foreground sm:inline">
            {isAutoProvider(m.provider) ? modelAssignmentLabel(m) : m.note}
            {m.usedByCopilot && (
              <span className="ml-2 rounded-full border border-primary/40 bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">
                Copilot
              </span>
            )}
          </span>
        </label>
      ))}
    </div>
  );
}

function ConnectModelInline({
  providers,
  onCreated,
}: {
  providers: ModelProviderDTO[];
  onCreated: (model: ModelDTO) => void;
}) {
  const t = useT();
  const createModel = useCreateModel();
  const [provider, setProvider] = useState(providers[0]?.canonical ?? "");
  const [modelTag, setModelTag] = useState("");
  const [locality, setLocality] = useState<"cloud" | "local">("cloud");
  const [displayName, setDisplayName] = useState("");

  const submit = async () => {
    if (!provider || !modelTag.trim()) {
      toast.error(
        t("Provider and model tag are required", "Anbieter und Modell-Tag sind erforderlich"),
      );
      return;
    }
    try {
      const model = await createModel.mutateAsync({
        provider,
        model: modelTag.trim(),
        locality,
        displayName: displayName.trim() || undefined,
      });
      toast.success(t(`${modelTag} connected`, `${modelTag} verbunden`));
      onCreated(model);
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : t("Could not connect model", "Modell konnte nicht verbunden werden"),
      );
    }
  };

  return (
    <div className="space-y-3 rounded-lg border border-border bg-background/30 p-4">
      <p className="text-sm text-muted-foreground">
        {t(
          "No model connected yet — connect one now to give this agent something to think with.",
          "Noch kein Modell verbunden — verbinde jetzt eines, damit dieser Agent etwas hat, womit er denken kann.",
        )}
      </p>
      <label className="block">
        <span className="mb-1 block text-[10px] uppercase tracking-widest text-muted-foreground">
          {t("Provider", "Anbieter")}
        </span>
        <select
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
          className="w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm"
        >
          {providers
            .filter((p) => p.canonical !== AUTO_PROVIDER)
            .map((p) => (
              <option key={p.canonical} value={p.canonical}>
                {p.canonical} ({p.locality})
              </option>
            ))}
        </select>
      </label>
      <label className="block">
        <span className="mb-1 block text-[10px] uppercase tracking-widest text-muted-foreground">
          {t("Model tag", "Modell-Tag")}
        </span>
        <input
          value={modelTag}
          onChange={(e) => setModelTag(e.target.value)}
          placeholder="llama3.1:8b / claude-3-5-sonnet-20241022"
          className="w-full rounded-md border border-border bg-background/40 px-3 py-2 font-mono text-xs"
        />
      </label>
      <label className="block">
        <span className="mb-1 block text-[10px] uppercase tracking-widest text-muted-foreground">
          {t("Locality", "Standort")}
        </span>
        <select
          value={locality}
          onChange={(e) => setLocality(e.target.value as "cloud" | "local")}
          className="w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm"
        >
          <option value="cloud">{t("Cloud", "Cloud")}</option>
          <option value="local">{t("Local", "Lokal")}</option>
        </select>
      </label>
      <label className="block">
        <span className="mb-1 block text-[10px] uppercase tracking-widest text-muted-foreground">
          {t("Display name (optional)", "Anzeigename (optional)")}
        </span>
        <input
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          placeholder="e.g. Sales GPT"
          className="w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm"
        />
      </label>
      <button
        type="button"
        onClick={submit}
        disabled={createModel.isPending}
        className="w-full rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
      >
        {createModel.isPending
          ? t("Connecting…", "Wird verbunden …")
          : t("Connect model", "Modell verbinden")}
      </button>
    </div>
  );
}
