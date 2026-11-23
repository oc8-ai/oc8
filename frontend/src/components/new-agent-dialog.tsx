import { fill } from "@/components/connections/texts";
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import {
  Check,
  ChevronLeft,
  ChevronRight,
  Clock,
  Plug,
  Shield,
  Sparkles,
  Trash2,
  User,
  Webhook,
  Wrench,
  X,
  Zap,
} from "lucide-react";
import { toast } from "sonner";
import {
  AgentIdentityFields,
  AVATAR_COLORS,
  Field,
  type AgentIdentity,
} from "@/components/agent-identity-fields";
import { AgentAvatar, hueFromOklch } from "@/components/agent-avatar";
import { AddToolPicker } from "@/components/add-tool-picker";
import { ModelPicker } from "@/components/model-picker";
import { CronBuilder } from "@/components/cron-builder";
import { CredentialPicker } from "@/components/credential-picker";
import {
  useCreateAgent,
  useCreateAgentTrigger,
  useCreateMcpLogin,
  useDepartments,
  useDepartmentTools,
  useMcpConnections,
  useModelProviders,
  useModels,
} from "@/lib/hooks";
import { useSetAgentNarrowing } from "@/lib/hooks-agent-detail";
import { usePublishHireFollowUps } from "@/lib/agent-versions";
import { useT } from "@/lib/i18n";
import type { GuardrailValue } from "@/components/guardrail-preset-picker";
import { mcpToolsFromConnections, type McpTool } from "@/lib/permissions";
import { thresholdField, valueSpecByName } from "@/lib/value-spec";
import { useOrgTimezone } from "@/lib/timezones";
import { cn } from "@/lib/utils";

// A tool's real guardrail is its per-tool approval threshold in `narrowing`,
// not a free-text field -- see new-agent-dialog's "Guardrail" label below.
const DEFAULT_CRON = "0 9 * * 1-5";

function wizardSteps(t: (en: string, de?: string) => string) {
  return [
    {
      key: "identity",
      title: t("Name & Role", "Name & Rolle"),
      subtitle: t("Who is this agent?", "Wer ist dieser Agent?"),
      icon: User,
    },
    {
      key: "model",
      title: t("Choose LLM", "LLM wählen"),
      subtitle: t("Which model should think?", "Welches Modell soll denken?"),
      icon: Sparkles,
    },
    {
      key: "tools",
      title: t("Tools (MCP)", "Tools (MCP)"),
      subtitle: t("What should it work with?", "Womit soll er arbeiten?"),
      icon: Wrench,
    },
    {
      key: "launch",
      title: t("Guardrails & Trigger", "Guardrails & Auslöser"),
      subtitle: t("Rules, then go live.", "Regeln, dann live."),
      icon: Shield,
    },
  ];
}

export function NewAgentDialog({
  open,
  onOpenChange,
  defaultDepartmentId,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  // Pre-selects the department step when opened from a department's own
  // page (the "Hire" button there) instead of falling back to whichever
  // department happens to load first -- still just a default, the picker
  // itself stays open to change.
  defaultDepartmentId?: string;
}) {
  const { data: models = [] } = useModels();
  const { data: providers = [] } = useModelProviders();
  // The tools a new agent CAN be handed are the tenant's real MCP
  // connections -- never a static vendor catalog (see permissions-panel.tsx,
  // which sources the same way). Selecting a tool here grants it through the
  // agent's narrowing (see `frameState`) and also lists it on
  // `presentation.tools` for the profile card / summary.
  const { data: connections = [] } = useMcpConnections();
  const mcpTools = useMemo(() => mcpToolsFromConnections(connections), [connections]);
  const valueSpecs = useMemo(() => valueSpecByName(connections), [connections]);
  const connectedNames = useMemo(
    () => new Set(connections.filter((c) => c.connected).map((c) => c.name)),
    [connections],
  );
  // Department picker for the new agent, not a paginated list view.
  const { data: departmentsPage } = useDepartments({ pageSize: 200 });
  const departments = departmentsPage?.items ?? [];
  const createAgent = useCreateAgent();
  const createTrigger = useCreateAgentTrigger();
  const setNarrowing = useSetAgentNarrowing();
  const publishHire = usePublishHireFollowUps();
  const navigate = useNavigate();
  const t = useT();
  const steps = wizardSteps(t);

  const [step, setStep] = useState(0);
  const [identity, setIdentity] = useState<AgentIdentity>({
    name: "",
    role: "",
    description: "",
    departmentId: "",
    avatarIdx: 0,
  });
  const [llm, setLlm] = useState<string>("");
  const [tools, setTools] = useState<string[]>([]);
  // The guardrails "Add tool" picked for a tool (the capa's recommendation,
  // or none): what a tool the department doesn't offer is granted with.
  const [toolPolicies, setToolPolicies] = useState<Record<string, GuardrailValue | null>>({});
  const [pickerOpen, setPickerOpen] = useState(false);
  const [trigger, setTrigger] = useState("on-demand");
  const [cronExpression, setCronExpression] = useState(DEFAULT_CRON);
  // The zone the schedule is read in: the organisation's unless picked.
  const orgTimezone = useOrgTimezone();
  const [pickedZone, setPickedZone] = useState<string | null>(null);
  const timezone = pickedZone ?? orgTimezone;
  const [guardrail, setGuardrail] = useState(() =>
    t("Approval from €5,000", "Freigabe ab 5.000 €"),
  );

  // Per-tool login choice + approval cap, keyed like `tools` -- the same
  // by-name convention NarrowingEditor uses (agents.$id.tsx): the tool key IS
  // the tool's connection name. Tool selection here GRANTS access via
  // `narrowing` at creation time; a picked login becomes the agent's fixed
  // access for the tool (POST /mcp/logins with the new agent's id) once the
  // agent exists. Without a pick the agent uses the login enabled for its
  // department.
  const { data: deptTools } = useDepartmentTools(identity.departmentId);
  const frameTools = (deptTools?.tools ?? {}) as Record<string, Record<string, unknown>>;
  const [credentialId, setCredentialId] = useState<Record<string, string>>({});
  // Picking (or creating) a login happens inline via CredentialPicker --
  // the same "select or create new" control the Capa setup form uses, not
  // a stacked modal, and no free-text scopes field (live user feedback:
  // asking for raw tool-call names "das bekommt doch kein mitarbeiter hin").
  const createLogin = useCreateMcpLogin();

  // Default the model / department selection once the lists load.
  useEffect(() => {
    if (!llm && models.length > 0) setLlm(models[0].id);
  }, [models, llm]);
  useEffect(() => {
    if (identity.departmentId || departments.length === 0) return;
    const preselected =
      defaultDepartmentId && departments.some((d) => d.id === defaultDepartmentId)
        ? defaultDepartmentId
        : departments[0].id;
    setIdentity((prev) => ({ ...prev, departmentId: preselected }));
  }, [departments, identity.departmentId, defaultDepartmentId]);

  // Deterministic-ish avatar color based on name
  useEffect(() => {
    if (!identity.name) return;
    const sum = [...identity.name].reduce((s, c) => s + c.charCodeAt(0), 0);
    const nextIdx = sum % AVATAR_COLORS.length;
    setIdentity((prev) => (prev.avatarIdx === nextIdx ? prev : { ...prev, avatarIdx: nextIdx }));
  }, [identity.name]);

  if (!open) return null;

  function toggleTool(id: string) {
    setTools((t) => (t.includes(id) ? t.filter((x) => x !== id) : [...t, id]));
  }

  function removeTool(id: string) {
    setTools((t) => t.filter((x) => x !== id));
    setToolPolicies((s) => {
      const next = { ...s };
      delete next[id];
      return next;
    });
    setCredentialId((s) => {
      const next = { ...s };
      delete next[id];
      return next;
    });
  }

  function reset() {
    setStep(0);
    setIdentity((prev) => ({ ...prev, name: "", role: "", description: "" }));
    setTools([]);
    setToolPolicies({});
    setLlm(models[0]?.id ?? "");
    setTrigger("on-demand");
    setCronExpression(DEFAULT_CRON);
    setCredentialId({});
  }

  function pickCredential(toolKey: string, picked: string) {
    setCredentialId((s) => {
      const next = { ...s };
      if (picked) next[toolKey] = picked;
      else delete next[toolKey];
      return next;
    });
  }

  // The picked logins become the new agent's fixed access, one per tool.
  async function fixLogins(agentId: string) {
    for (const [toolKey, picked] of Object.entries(credentialId)) {
      if (!tools.includes(toolKey)) continue;
      const credentialType = connections.find((c) => c.name === toolKey)?.credentialType;
      if (!credentialType) continue;
      try {
        await createLogin.mutateAsync({
          name: toolKey,
          credentialType,
          credentialId: picked,
          scopes: [],
          agentId,
        });
      } catch (err) {
        toast.error(
          t(
            "Agent hired, but its login could not be set",
            "Agent eingestellt, aber sein Zugang konnte nicht gesetzt werden",
          ),
          {
            description: `${toolKey}: ${err instanceof Error ? err.message : String(err)}`,
          },
        );
      }
    }
  }

  // A tool the department's frame enables is inherited as the frame sets it.
  // One the frame doesn't name at all is granted to this agent alone (an
  // agent-exclusive grant, `narrowing_within_frame`), with the guardrails
  // picked when adding it. One the frame names but turned off can't be
  // granted: a narrowing never widens the frame.
  function frameState(key: string): "enabled" | "absent" | "disabled" {
    const frame = frameTools[key];
    if (!frame) return "absent";
    return frame.enabled ? "enabled" : "disabled";
  }

  function inheritedNarrowing(): Record<string, unknown> {
    const toolsPayload: Record<string, unknown> = {};
    for (const key of tools) {
      if (frameState(key) !== "enabled") continue;
      const frame = frameTools[key];
      toolsPayload[key] = {
        enabled: true,
        read: !!frame.read,
        modify: !!frame.modify,
        ...thresholdField(
          key,
          (frame.approval_eur as number | null | undefined) ?? null,
          valueSpecs,
        ),
        connection_id: null,
      };
    }
    return toolsPayload;
  }

  function agentOnlyNarrowing(): Record<string, unknown> {
    const toolsPayload: Record<string, unknown> = {};
    for (const key of tools) {
      if (frameState(key) !== "absent") continue;
      const policy = toolPolicies[key] ?? null;
      toolsPayload[key] = {
        enabled: true,
        read: policy ? policy.read : true,
        modify: policy ? policy.modify : false,
        ...thresholdField(key, policy?.approvalEur ?? null, valueSpecs),
        approval_actions: policy?.approvalActions ?? [],
        only: policy?.only ?? [],
        conditions: policy?.conditions ?? [],
        limits: policy?.limits ?? [],
        connection_id: null,
      };
    }
    return toolsPayload;
  }

  async function finish() {
    const model = models.find((m) => m.id === llm);
    const selectedTools = mcpTools.filter((i) => tools.includes(i.id));

    // A tool the department turned off is left out of the grant (see
    // `frameState`). Silent would be the bug: a login was still picked and
    // pinned for it, so without the warning below the agent looks fully
    // configured and simply can't reach the tool the first time it tries.
    const notInDeptFrame = tools.filter((key) => frameState(key) === "disabled");
    const inherited = inheritedNarrowing();
    const agentOnly = agentOnlyNarrowing();

    try {
      const agent = await createAgent.mutateAsync({
        name: identity.name,
        departmentId: identity.departmentId,
        roleTitle: identity.role,
        mission: identity.description,
        modelConfigId: llm || null,
        narrowing: Object.keys(inherited).length > 0 ? { tools: inherited } : undefined,
        presentation: {
          provider: model?.provider ?? "Ollama",
          llm: model?.name ?? "",
          tools: selectedTools.map((i) => i.name),
          guardrails: guardrail ? [guardrail] : [],
          schedule: trigger,
          avatar_color: AVATAR_COLORS[identity.avatarIdx],
        },
      });
      await fixLogins(agent.id);
      // After the logins: a tool that takes one is granted only once the
      // agent's login for it is fixed.
      if (Object.keys(agentOnly).length > 0) {
        try {
          await setNarrowing.mutateAsync({
            agentId: agent.id,
            narrowing: { tools: { ...inherited, ...agentOnly } },
          });
        } catch (err) {
          toast.error(
            t(
              "Agent hired, but its tools could not be granted",
              "Agent eingestellt, aber seine Tools konnten nicht freigegeben werden",
            ),
            {
              description: `${Object.keys(agentOnly).join(", ")}: ${err instanceof Error ? err.message : String(err)}`,
            },
          );
        }
      }

      // The login pins and grants above are part of the hire: publish them
      // with it, so the new agent starts with no unpublished changes (E2E N9).
      try {
        await publishHire.mutateAsync({
          agentId: agent.id,
          expectedCurrentVersionNo: agent.currentVersionNo ?? null,
        });
      } catch {
        // Not fatal: the changes stay as a draft the agent page offers to publish.
      }

      // A real cron trigger needs a real agent id, so it can't ride in the
      // creation request -- fire it right after, same endpoint the agent
      // page's own ScheduleEditor uses (agents.$id.tsx).
      let webhookCreated = false;
      if (trigger === "schedule" && cronExpression.trim()) {
        try {
          await createTrigger.mutateAsync({
            agentId: agent.id,
            cronExpression,
            timezone,
            taskText: fill(t("Scheduled run for {name}", "Geplanter Lauf für {name}"), {
              name: identity.name,
            }),
          });
        } catch (err) {
          toast.error(
            t(
              "Agent hired, but the schedule could not be saved",
              "Agent eingestellt, aber der Zeitplan konnte nicht gespeichert werden",
            ),
            {
              description: String(err),
            },
          );
        }
      } else if (trigger === "webhook") {
        try {
          await createTrigger.mutateAsync({
            agentId: agent.id,
            kind: "webhook",
            taskText: fill(
              t(
                "React to whatever this webhook sends, on {name}'s behalf.",
                "Reagiere im Namen von {name} auf das, was dieser Webhook sendet.",
              ),
              { name: identity.name },
            ),
          });
          webhookCreated = true;
        } catch (err) {
          toast.error(
            t(
              "Agent hired, but the webhook could not be created",
              "Agent eingestellt, aber der Webhook konnte nicht angelegt werden",
            ),
            {
              description: String(err),
            },
          );
        }
      }

      toast.success(t("Agent hired", "Agent eingestellt"), {
        description: fill(
          t(
            "{name} is ready — {count} tools, {model}.",
            "{name} ist bereit — {count} Tools, {model}.",
          ),
          {
            name: identity.name || t("New hire", "Neuer Agent"),
            count: String(tools.length),
            model: model?.name ?? "",
          },
        ),
      });
      if (notInDeptFrame.length > 0) {
        toast.warning(
          t(
            "Some tools are turned off for this department",
            "Einige Tools sind für diese Abteilung abgeschaltet",
          ),
          {
            description: fill(
              notInDeptFrame.length === 1
                ? t(
                    "{tools}: {name} can use it once the department turns it on in its tool settings.",
                    "{tools}: {name} kann es nutzen, sobald die Abteilung es in ihren Tool-Einstellungen einschaltet.",
                  )
                : t(
                    "{tools}: {name} can use them once the department turns them on in its tool settings.",
                    "{tools}: {name} kann sie nutzen, sobald die Abteilung sie in ihren Tool-Einstellungen einschaltet.",
                  ),
              {
                tools: notInDeptFrame.join(", "),
                name: identity.name || t("the agent", "der Agent"),
              },
            ),
            duration: 10000,
          },
        );
      }
      reset();
      onOpenChange(false);
      // The webhook URL only exists on the agent's own page (TriggerEditor)
      // -- send the operator straight there to copy it, instead of also
      // rendering it inside this dialog.
      if (webhookCreated) navigate({ to: "/agents/$id", params: { id: agent.id } });
    } catch (err) {
      toast.error(t("Could not hire agent", "Agent konnte nicht eingestellt werden"), {
        description: String(err),
      });
    }
  }

  const canProceed =
    step === 0
      ? identity.name.trim().length > 0 &&
        identity.role.trim().length > 0 &&
        identity.departmentId.length > 0
      : true;

  const progress = ((step + 1) / steps.length) * 100;

  const selectedModel = models.find((m) => m.id === llm);
  const activeStep = steps[step];
  const StepIcon = activeStep.icon;

  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur-md animate-in fade-in duration-200"
      onClick={(e) => e.target === e.currentTarget && onOpenChange(false)}
    >
      <div className="w-full max-w-3xl overflow-hidden rounded-2xl border border-border bg-panel shadow-2xl animate-in fade-in zoom-in-95 duration-200">
        {/* Header */}
        <header className="relative border-b border-border px-6 pb-5 pt-6">
          <div className="flex items-start justify-between gap-4">
            <div className="flex items-center gap-3">
              <div className="grid h-11 w-11 place-items-center rounded-xl bg-primary/15 text-primary glow-teal">
                <StepIcon className="h-5 w-5" />
              </div>
              <div>
                <h3 className="font-serif text-2xl leading-none">
                  {t("Hire a new agent", "Neuen Agenten einstellen")}
                </h3>
                <p className="mt-1.5 text-xs text-muted-foreground">
                  {fill(t("Step {step} of {total}", "Schritt {step} von {total}"), {
                    step: String(step + 1),
                    total: String(steps.length),
                  })}{" "}
                  · {activeStep.subtitle}
                </p>
              </div>
            </div>
            <button
              onClick={() => onOpenChange(false)}
              className="grid h-8 w-8 shrink-0 place-items-center rounded-md text-muted-foreground transition hover:bg-background/40 hover:text-foreground"
              aria-label={t("Close", "Schließen")}
            >
              <X className="h-4 w-4" />
            </button>
          </div>

          {/* Progress bar */}
          <div className="mt-5">
            <div className="relative h-1.5 w-full overflow-hidden rounded-full bg-background/60">
              <div
                className="absolute inset-y-0 left-0 rounded-full bg-primary transition-[width] duration-500 ease-out"
                style={{
                  width: `${progress}%`,
                  boxShadow: "0 0 12px color-mix(in oklab, var(--primary) 60%, transparent)",
                }}
              />
            </div>
            <ol className="mt-3 grid grid-cols-4 gap-2">
              {steps.map((s, i) => (
                <li key={s.key} className="flex min-w-0 items-center gap-2">
                  <span
                    className={cn(
                      "grid h-5 w-5 shrink-0 place-items-center rounded-full text-[10px] font-semibold transition",
                      i < step
                        ? "bg-primary text-primary-foreground"
                        : i === step
                          ? "bg-primary/20 text-primary ring-1 ring-primary"
                          : "bg-background/60 text-muted-foreground",
                    )}
                  >
                    {i < step ? <Check className="h-3 w-3" /> : i + 1}
                  </span>
                  <span
                    className={cn(
                      "truncate text-[11px]",
                      i === step
                        ? "text-foreground"
                        : i < step
                          ? "text-muted-foreground"
                          : "text-muted-foreground/60",
                    )}
                  >
                    {s.title}
                  </span>
                </li>
              ))}
            </ol>
          </div>
        </header>

        {/* Body */}
        <div
          key={step}
          className="max-h-[62vh] space-y-4 overflow-y-auto px-6 py-6 animate-in fade-in slide-in-from-right-2 duration-300"
        >
          {step === 0 && (
            <AgentIdentityFields
              value={identity}
              onChange={setIdentity}
              departments={departments}
            />
          )}

          {step === 1 && (
            <ModelPicker
              models={models}
              providers={providers}
              selectedId={llm}
              onSelect={(id) => setLlm(id)}
            />
          )}

          {step === 2 && (
            <div className="space-y-4">
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span className="inline-flex items-center gap-1.5">
                  <Plug className="h-3.5 w-3.5 text-primary" />
                  {fill(
                    tools.length === 1
                      ? t("{count} tool selected", "{count} Tool ausgewählt")
                      : t("{count} tools selected", "{count} Tools ausgewählt"),
                    { count: String(tools.length) },
                  )}
                </span>
                <button
                  type="button"
                  onClick={() => setPickerOpen(true)}
                  disabled={connections.filter((c) => !tools.includes(c.name)).length === 0}
                  className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background/40 px-2.5 py-1.5 text-xs font-medium text-foreground transition hover:bg-background/70 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <Wrench className="h-3.5 w-3.5" /> {t("Add tool", "Tool hinzufügen")}
                </button>
              </div>
              {mcpTools.length === 0 && (
                <p className="rounded-md border border-dashed border-border px-3 py-2 text-xs text-muted-foreground">
                  {t(
                    "No MCP connections yet. Add one under Capas to offer it here.",
                    "Noch keine MCP-Verbindungen. Füge unter Capas eine hinzu, um sie hier anzubieten.",
                  )}
                </p>
              )}
              {tools.length === 0 && mcpTools.length > 0 && (
                <p className="rounded-md border border-dashed border-border px-3 py-2 text-xs text-muted-foreground">
                  {t(
                    'No tools selected yet — click "Add tool" to pick one.',
                    'Noch keine Tools ausgewählt — klicke auf "Tool hinzufügen", um eines zu wählen.',
                  )}
                </p>
              )}
              <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3">
                {mcpTools
                  .filter((i) => tools.includes(i.id))
                  .map((i) => {
                    const Icon = i.icon;
                    const inFrame = frameState(i.id);
                    const credentialType = connections.find((c) => c.name === i.id)?.credentialType;
                    const pickedCredentialId = credentialId[i.id] ?? "";
                    return (
                      <div
                        key={i.id}
                        className="group relative flex flex-col items-start gap-2 rounded-xl border border-primary bg-primary/10 glow-teal p-3 text-left"
                      >
                        <div className="flex w-full items-center justify-between">
                          <div className="grid h-9 w-9 place-items-center rounded-lg bg-primary/15 text-primary">
                            <Icon className="h-4 w-4" />
                          </div>
                          <button
                            type="button"
                            onClick={() => removeTool(i.id)}
                            title={t("Remove tool", "Tool entfernen")}
                            className="grid h-6 w-6 place-items-center rounded-md text-muted-foreground transition hover:text-destructive"
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        </div>
                        <div className="w-full min-w-0">
                          <div className="truncate text-sm font-medium">{i.name}</div>
                          <div className="truncate text-[10px] text-muted-foreground">
                            {connectedNames.has(i.id)
                              ? t("via MCP", "über MCP")
                              : t("not connected", "nicht verbunden")}
                          </div>
                        </div>
                        {credentialType && (
                          <div className="w-full min-w-0 border-t border-border/60 pt-2">
                            <CredentialPicker
                              credentialType={credentialType}
                              value={pickedCredentialId}
                              onChange={(picked) => pickCredential(i.id, picked)}
                            />
                          </div>
                        )}
                        {inFrame !== "enabled" && (
                          <p className="text-[10px] text-muted-foreground">
                            {inFrame === "absent"
                              ? t(
                                  "Only for this agent: the department doesn't offer this tool, so it is granted to this agent alone.",
                                  "Nur für diesen Agenten: Die Abteilung bietet dieses Tool nicht an, darum bekommt es nur dieser Agent.",
                                )
                              : t(
                                  "Turned off for this department: the agent can use it once the department turns it on.",
                                  "Für diese Abteilung abgeschaltet: Der Agent kann es nutzen, sobald die Abteilung es einschaltet.",
                                )}
                          </p>
                        )}
                      </div>
                    );
                  })}
              </div>
            </div>
          )}

          {pickerOpen && (
            <AddToolPicker
              addableNames={connections.filter((c) => !tools.includes(c.name)).map((c) => c.name)}
              connections={connections}
              onAdd={(name, policy) => {
                toggleTool(name);
                setToolPolicies((prev) => ({ ...prev, [name]: policy }));
                setPickerOpen(false);
              }}
              onClose={() => setPickerOpen(false)}
            />
          )}

          {step === 3 && (
            <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,320px)]">
              <div className="space-y-4">
                <Field label={t("Guardrail label", "Guardrail-Beschriftung")}>
                  <input
                    value={guardrail}
                    onChange={(e) => setGuardrail(e.target.value)}
                    placeholder={t(
                      "A short note for the profile card",
                      "Eine kurze Notiz für die Profilkarte",
                    )}
                    className="w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
                  />
                  <p className="mt-1 text-[11px] text-muted-foreground">
                    {t(
                      "Just a label for this card — it doesn't change what the agent can actually do.",
                      "Nur eine Beschriftung für diese Karte — sie ändert nicht, was der Agent tatsächlich darf.",
                    )}
                  </p>
                </Field>
                <Field label={t("Trigger", "Auslöser")}>
                  <div className="grid gap-2 sm:grid-cols-3">
                    {[
                      {
                        id: "on-demand",
                        label: t("On-Demand", "Bei Bedarf"),
                        icon: Zap,
                        disabled: false,
                      },
                      {
                        id: "schedule",
                        label: t("Schedule", "Zeitplan"),
                        icon: Clock,
                        disabled: false,
                      },
                      {
                        id: "webhook",
                        label: t("Webhook", "Webhook"),
                        icon: Webhook,
                        disabled: false,
                      },
                    ].map((option) => {
                      const Icon = option.icon;
                      const on = trigger === option.id;
                      return (
                        <button
                          key={option.id}
                          type="button"
                          onClick={() => !option.disabled && setTrigger(option.id)}
                          disabled={option.disabled}
                          className={cn(
                            "inline-flex flex-col items-center gap-1.5 rounded-md border px-3 py-3 text-xs transition",
                            on
                              ? "border-primary bg-primary/10 text-primary"
                              : "border-border bg-background/30 text-muted-foreground hover:border-primary/40 hover:text-foreground",
                            option.disabled && "cursor-not-allowed opacity-50 hover:border-border",
                          )}
                        >
                          <Icon className="h-4 w-4" />
                          {option.label}
                        </button>
                      );
                    })}
                  </div>
                  {trigger === "schedule" && (
                    <div className="mt-3">
                      <CronBuilder
                        initial={cronExpression}
                        onChange={setCronExpression}
                        onSave={() =>
                          toast.success(t("Schedule set", "Zeitplan gesetzt"), {
                            description: t(
                              "Saved once you hire the agent.",
                              "Wird gespeichert, sobald du den Agenten einstellst.",
                            ),
                          })
                        }
                        saveLabel={t("Looks good", "Passt so")}
                        timezone={timezone}
                        onTimezoneChange={setPickedZone}
                      />
                    </div>
                  )}
                  {trigger === "webhook" && (
                    <p className="mt-3 rounded-md border border-dashed border-border px-3 py-2 text-[11px] text-muted-foreground">
                      {t(
                        "A unique webhook URL is generated once the agent is hired. You'll land on the agent's page to copy it — Odoo's Automation Rules or any other tool that can send a POST request works.",
                        "Sobald der Agent eingestellt ist, wird eine eigene Webhook-URL erzeugt. Du landest auf der Seite des Agenten, um sie zu kopieren — Odoos Automatisierungsregeln oder jedes andere Tool, das einen POST-Request senden kann, funktioniert.",
                      )}
                    </p>
                  )}
                </Field>
              </div>

              {/* Profile card summary */}
              <ProfileCard
                name={identity.name}
                role={identity.role}
                description={identity.description}
                avatarColor={AVATAR_COLORS[identity.avatarIdx]}
                llm={selectedModel?.name ?? ""}
                provider={selectedModel?.provider ?? ""}
                trigger={trigger}
                guardrail={guardrail}
                toolIds={tools}
                mcpTools={mcpTools}
              />
            </div>
          )}
        </div>

        <footer className="flex items-center justify-between gap-3 border-t border-border bg-background/30 px-6 py-4">
          <button
            onClick={() => (step === 0 ? onOpenChange(false) : setStep(step - 1))}
            className="inline-flex items-center gap-1 rounded-md px-3 py-1.5 text-sm text-muted-foreground transition hover:text-foreground"
          >
            {step === 0 ? (
              t("Cancel", "Abbrechen")
            ) : (
              <>
                <ChevronLeft className="h-4 w-4" /> {t("Back", "Zurück")}
              </>
            )}
          </button>
          {step < steps.length - 1 ? (
            <button
              onClick={() => canProceed && setStep(step + 1)}
              disabled={!canProceed}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-md px-4 py-2 text-sm font-medium transition",
                canProceed
                  ? "bg-primary text-primary-foreground hover:brightness-110 glow-teal"
                  : "cursor-not-allowed bg-background/40 text-muted-foreground",
              )}
            >
              {t("Next", "Weiter")} <ChevronRight className="h-4 w-4" />
            </button>
          ) : (
            <button
              onClick={finish}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:brightness-110 glow-teal"
            >
              <Check className="h-4 w-4" /> {t("Hire agent", "Agent einstellen")}
            </button>
          )}
        </footer>
      </div>
    </div>
  );
}

function ProfileCard({
  name,
  role,
  description,
  avatarColor,
  llm,
  provider,
  trigger,
  guardrail,
  toolIds,
  mcpTools,
}: {
  name: string;
  role: string;
  description: string;
  avatarColor: string;
  llm: string;
  provider: string;
  trigger: string;
  guardrail: string;
  toolIds: string[];
  mcpTools: McpTool[];
}) {
  const t = useT();
  const selected = useMemo(
    () => mcpTools.filter((i) => toolIds.includes(i.id)),
    [mcpTools, toolIds],
  );
  const triggerLabel =
    trigger === "on-demand"
      ? t("On-Demand", "Bei Bedarf")
      : trigger === "schedule"
        ? t("Schedule", "Zeitplan")
        : t("Webhook", "Webhook");

  return (
    <div className="relative overflow-hidden rounded-2xl border border-primary/40 bg-gradient-to-b from-primary/10 via-panel to-panel p-5 shadow-[0_0_0_1px_var(--primary)]/10">
      <div className="pointer-events-none absolute -right-10 -top-10 h-40 w-40 rounded-full bg-primary/15 blur-3xl" />
      <div className="mb-4 flex items-center justify-between text-[10px] uppercase tracking-widest text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <Sparkles className="h-3 w-3 text-primary" />
          {t("Agent profile", "Agentenprofil")}
        </span>
        <span className="rounded-full border border-primary/40 bg-primary/10 px-2 py-0.5 text-[9px] text-primary">
          {t("ready to launch", "startklar")}
        </span>
      </div>

      <div className="flex items-center gap-3">
        <AgentAvatar
          seed={name.trim() || "agent"}
          size={56}
          background="squircle"
          hue={hueFromOklch(avatarColor)}
        />
        <div className="min-w-0">
          <div className="truncate font-serif text-2xl leading-none">
            {name || t("New agent", "Neuer Agent")}
          </div>
          <div className="mt-1 truncate text-xs text-muted-foreground">
            {role || t("Role …", "Rolle …")}
          </div>
        </div>
      </div>

      {description && (
        <p className="mt-3 line-clamp-3 border-l-2 border-primary/40 pl-3 text-xs italic text-muted-foreground">
          "{description}"
        </p>
      )}

      <dl className="mt-4 space-y-2.5 text-xs">
        <Row icon={<Sparkles className="h-3 w-3" />} label="LLM">
          <span className="font-medium text-foreground">{llm}</span>
          <span className="text-muted-foreground"> · {provider}</span>
        </Row>
        <Row icon={<Zap className="h-3 w-3" />} label={t("Trigger", "Auslöser")}>
          <span className="font-medium text-foreground">{triggerLabel}</span>
        </Row>
        <Row icon={<Shield className="h-3 w-3" />} label="Guardrail">
          <span className="text-foreground">{guardrail || "—"}</span>
        </Row>
      </dl>

      <div className="mt-4">
        <div className="mb-1.5 text-[10px] uppercase tracking-widest text-muted-foreground">
          {fill(t("Tools ({count})", "Tools ({count})"), { count: String(selected.length) })}
        </div>
        {selected.length === 0 ? (
          <div className="rounded-md border border-dashed border-border px-2 py-1.5 text-[11px] text-muted-foreground">
            {t("No tools selected yet.", "Noch keine Tools ausgewählt.")}
          </div>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {selected.map((i) => {
              const Icon = i.icon;
              return (
                <span
                  key={i.id}
                  className="inline-flex items-center gap-1 rounded-full border border-border bg-background/40 px-2 py-0.5 text-[10px]"
                >
                  <Icon className="h-3 w-3 text-primary" />
                  {i.name}
                </span>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

function Row({
  icon,
  label,
  children,
}: {
  icon: React.ReactNode;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="grid grid-cols-[auto_60px_minmax(0,1fr)] items-center gap-2">
      <span className="grid h-5 w-5 place-items-center rounded-md bg-background/40 text-muted-foreground">
        {icon}
      </span>
      <span className="text-[10px] uppercase tracking-widest text-muted-foreground">{label}</span>
      <span className="truncate">{children}</span>
    </div>
  );
}
