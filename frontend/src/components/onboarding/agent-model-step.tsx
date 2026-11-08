import { useState } from "react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { useT } from "@/lib/i18n";
import {
  useCreateAgent,
  useModelProviders,
  useModels,
  useRuntimes,
  type AgentDetail,
  type ModelDTO,
} from "@/lib/hooks";
import { ModelPicker } from "@/components/model-picker";
import { RuntimePicker } from "@/components/runtime-picker";
import { AVATAR_COLORS, type AgentIdentity } from "@/components/agent-identity-fields";
// Reused as-is (not re-implemented) so onboarding connects a provider through
// the exact same credential flow as Settings -> Models: a bare
// {provider, model, locality} POST with no credential attached, which is
// what onboarding used to do here, never actually authenticates against the
// provider.
import { AddProviderWizard } from "@/routes/models";

export function AgentModelStep({
  identity,
  onDone,
}: {
  identity: AgentIdentity;
  onDone: (agent: AgentDetail) => void;
}) {
  const t = useT();
  const { data: models = [] } = useModels();
  const { data: providers = [] } = useModelProviders();
  // A failed or still-loading fetch just leaves this `[]`, same as `models`
  // and `providers` above -- `RuntimePicker` renders nothing for an empty
  // list, and `runtimePluginId` stays `null` below, which is exactly what an
  // omitted field on `POST /agents` already means. So this never blocks the
  // hire the way a failed department-tools fetch blocks Guardrails.
  const { data: runtimes = [] } = useRuntimes();
  const createAgent = useCreateAgent();
  const [selectedId, setSelectedId] = useState("");
  const [runtimePluginId, setRuntimePluginId] = useState<string | null>(null);
  const [wizardOpen, setWizardOpen] = useState(false);

  const handleModelCreated = (model: ModelDTO) => {
    setSelectedId(model.id);
  };

  const create = async () => {
    const model = models.find((m) => m.id === selectedId);
    try {
      const agent = await createAgent.mutateAsync({
        name: identity.name,
        departmentId: identity.departmentId,
        roleTitle: identity.role,
        mission: identity.description,
        modelConfigId: selectedId || null,
        runtimePluginId,
        presentation: {
          provider: model?.provider ?? "",
          llm: model?.name ?? "",
          tools: [],
          guardrails: [],
          schedule: "on-demand",
          avatar_color: AVATAR_COLORS[identity.avatarIdx],
        },
      });
      toast.success(t("Agent created", "Agent erstellt"));
      onDone(agent);
    } catch (err) {
      toast.error(t("Could not create agent", "Agent konnte nicht erstellt werden"), {
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  return (
    <Panel className="space-y-4 p-6">
      <div>
        <h3 className="font-serif text-lg">
          {t("What powers this agent?", "Was treibt diesen Agenten an?")}
        </h3>
        <p className="mt-1 text-sm text-muted-foreground">
          {t(
            "Choose the model it thinks with and, if more than one is installed, the runtime it works in.",
            "Wähle das Modell, mit dem er denkt, und, falls mehr als eine installiert ist, die Runtime, in der er arbeitet.",
          )}
        </p>
      </div>
      {models.length > 0 ? (
        <>
          <ModelPicker
            models={models}
            providers={providers}
            selectedId={selectedId}
            onSelect={setSelectedId}
          />
          <button
            type="button"
            onClick={() => setWizardOpen(true)}
            className="text-xs text-primary hover:underline"
          >
            {t("Connect another provider", "Weiteren Anbieter verbinden")}
          </button>
        </>
      ) : (
        <div className="space-y-3 rounded-lg border border-dashed border-border bg-background/30 p-4 text-center">
          <p className="text-sm text-muted-foreground">
            {t(
              "No model connected yet — connect a provider now to give this agent something to think with.",
              "Noch kein Modell verbunden — verbinde jetzt einen Anbieter, damit dieser Agent etwas hat, womit er denken kann.",
            )}
          </p>
          <button
            type="button"
            onClick={() => setWizardOpen(true)}
            className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:brightness-110"
          >
            {t("Connect a provider", "Anbieter verbinden")}
          </button>
        </div>
      )}
      <RuntimePicker value={runtimePluginId} onChange={setRuntimePluginId} runtimes={runtimes} />
      <button
        onClick={create}
        disabled={!selectedId || createAgent.isPending}
        className="w-full rounded-md bg-brand-gradient px-4 py-2 text-sm font-medium text-primary-foreground transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-60"
      >
        {createAgent.isPending
          ? t("Creating…", "Wird erstellt …")
          : t("Create agent", "Agent erstellen")}
      </button>
      {wizardOpen && (
        <AddProviderWizard onClose={() => setWizardOpen(false)} onCreated={handleModelCreated} />
      )}
    </Panel>
  );
}
