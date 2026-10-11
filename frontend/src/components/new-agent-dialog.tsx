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
  useMcpLogins,
  useModelProviders,
  useModels,
  type McpLoginDTO,
} from "@/lib/hooks";
import { mcpToolsFromConnections, type McpTool } from "@/lib/permissions";
import { cn } from "@/lib/utils";

// A tool's real guardrail is its per-tool approval threshold in `narrowing`,
// not a free-text field -- see new-agent-dialog's "Guardrail" label below.
const DEFAULT_CRON = "0 9 * * 1-5";

const steps = [
  { title: "Name & Role", subtitle: "Who is this agent?", icon: User },
  { title: "Choose LLM", subtitle: "Which model should think?", icon: Sparkles },
  { title: "Tools (MCP)", subtitle: "What should it work with?", icon: Wrench },
  { title: "Guardrails & Trigger", subtitle: "Rules, then go live.", icon: Shield },
] as const;

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
  // which sources the same way). Selecting a tool here is descriptive only
  // (stored on `presentation.tools` for the profile card / summary) -- actual
  // access is granted afterwards via the department frame + agent narrowing.
  const { data: connections = [] } = useMcpConnections();
  const mcpTools = useMemo(() => mcpToolsFromConnections(connections), [connections]);
  const connectedNames = useMemo(
    () => new Set(connections.filter((c) => c.connected).map((c) => c.name)),
    [connections],
  );
  // Department picker for the new agent, not a paginated list view.
  const { data: departmentsPage } = useDepartments({ pageSize: 200 });
  const departments = departmentsPage?.items ?? [];
  const createAgent = useCreateAgent();
  const createTrigger = useCreateAgentTrigger();
  const navigate = useNavigate();

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
  const [pickerOpen, setPickerOpen] = useState(false);
  const [trigger, setTrigger] = useState("on-demand");
  const [cronExpression, setCronExpression] = useState(DEFAULT_CRON);
  const [guardrail, setGuardrail] = useState("Approval from €5,000");

  // Per-tool login pin + approval cap, keyed like `tools` -- the same by-name
  // convention NarrowingEditor uses (agents.$id.tsx): `McpConnection.name`
  // IS the tool key. This is what makes tool selection here actually GRANT
  // access via `narrowing` at creation time, instead of only decorating the
  // profile card the way it did before.
  const logins = useMcpLogins();
  // Only logins this agent could actually use: tenant-wide ones
  // (departmentId === null, the pre-existing default) plus ones scoped to
  // this agent's own department -- never another department's login, even
  // if it happens to share this tool key (agent tool login selection
  // design's department-scoping extension).
  const loginsByKey: Record<string, McpLoginDTO[]> = {};
  for (const login of logins.data ?? []) {
    if (login.departmentId !== null && login.departmentId !== identity.departmentId) continue;
    (loginsByKey[login.name] ??= []).push(login);
  }
  const { data: deptTools } = useDepartmentTools(identity.departmentId);
  const frameTools = (deptTools?.tools ?? {}) as Record<string, Record<string, unknown>>;
  const [connectionId, setConnectionId] = useState<Record<string, string>>({});
  // Picking (or creating) a login happens inline via CredentialPicker --
  // the same "select or create new" control the Capa setup form uses, not
  // a stacked modal, and no free-text scopes field (live user feedback:
  // asking for raw tool-call names "das bekommt doch kein mitarbeiter hin").
  // A login is a Credential paired 1:1 with a McpConnection (POST
  // /mcp/logins), department-scoped by default here, so picking a
  // credential still needs one created/reused behind the scenes --
  // pinCredential below does that.
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
    setConnectionId((s) => {
      const next = { ...s };
      delete next[id];
      return next;
    });
  }

  function reset() {
    setStep(0);
    setIdentity((prev) => ({ ...prev, name: "", role: "", description: "" }));
    setTools([]);
    setLlm(models[0]?.id ?? "");
    setTrigger("on-demand");
    setCronExpression(DEFAULT_CRON);
    setConnectionId({});
  }

  // Resolves a picked/created credential to a login (McpConnection) and
  // pins it for this tool key. Reuses an existing login already backed by
  // this exact credential; otherwise creates one -- POST /mcp/logins 409s
  // if a DIFFERENT credential already backs a login under this tool key,
  // which surfaces as the toast below (switching credentials isn't
  // supported yet, an edge case no capa here hits today).
  async function pinCredential(toolKey: string, credentialType: string, credentialId: string) {
    if (!credentialId) {
      setConnectionId((s) => {
        const next = { ...s };
        delete next[toolKey];
        return next;
      });
      return;
    }
    const existing = (loginsByKey[toolKey] ?? []).find((l) => l.credentialId === credentialId);
    if (existing) {
      setConnectionId((s) => ({ ...s, [toolKey]: existing.id }));
      return;
    }
    try {
      const login = await createLogin.mutateAsync({
        name: toolKey,
        credentialType,
        credentialId,
        scopes: [],
        // Scoped to this agent's own department, not tenant-wide: lets a
        // second department pin a different login under the same tool key
        // (e.g. two distinct Odoo logins) instead of hitting the "already
        // exists" conflict a tenant-wide login would.
        departmentId: identity.departmentId || undefined,
      });
      setConnectionId((s) => ({ ...s, [toolKey]: login.id }));
    } catch (err) {
      toast.error("Could not link this credential", {
        description: err instanceof Error ? err.message : String(err),
      });
    }
  }

  // Only tool keys the target department's frame actually enables become
  // real access -- narrowing can only tighten within the frame (never widen
  // it), so a tile selected outside the frame stays presentation-only, same
  // as every tool did before this feature.
  function buildNarrowing(): Record<string, unknown> | undefined {
    const toolsPayload: Record<string, unknown> = {};
    for (const key of tools) {
      const frame = frameTools[key];
      if (!frame || !frame.enabled) continue;
      const hasLogins = (loginsByKey[key]?.length ?? 0) > 0;
      toolsPayload[key] = {
        enabled: true,
        read: !!frame.read,
        modify: !!frame.modify,
        approval_eur: (frame.approval_eur as number | null | undefined) ?? null,
        connection_id: hasLogins ? connectionId[key] || null : null,
      };
    }
    return Object.keys(toolsPayload).length > 0 ? { tools: toolsPayload } : undefined;
  }

  async function finish() {
    const model = models.find((m) => m.id === llm);
    const selectedTools = mcpTools.filter((i) => tools.includes(i.id));

    for (const key of tools) {
      const frame = frameTools[key];
      const hasLogins = (loginsByKey[key]?.length ?? 0) > 0;
      if (frame?.enabled && hasLogins && !connectionId[key]) {
        toast.error("Pick a login before hiring", { description: key });
        return;
      }
    }

    // A tool picked here only becomes real access once the department's own
    // frame already enables it -- buildNarrowing() silently leaves these out
    // of the request, matching "narrowing can only tighten, never widen"
    // (see its own comment). Silent is the bug: a login was still picked and
    // pinned for it, so without this the agent looks fully configured and
    // simply can't reach the tool the first time it tries -- surfaced live
    // when a freshly hired agent had an Odoo login pinned in this dialog but
    // no access at run time, department frame not enabled for it.
    const notInDeptFrame = tools.filter((key) => !frameTools[key]?.enabled);

    try {
      const agent = await createAgent.mutateAsync({
        name: identity.name,
        departmentId: identity.departmentId,
        roleTitle: identity.role,
        mission: identity.description,
        modelConfigId: llm || null,
        narrowing: buildNarrowing(),
        presentation: {
          provider: model?.provider ?? "Ollama",
          llm: model?.name ?? "",
          tools: selectedTools.map((i) => i.name),
          guardrails: guardrail ? [guardrail] : [],
          schedule: trigger,
          avatar_color: AVATAR_COLORS[identity.avatarIdx],
        },
      });

      // A real cron trigger needs a real agent id, so it can't ride in the
      // creation request -- fire it right after, same endpoint the agent
      // page's own ScheduleEditor uses (agents.$id.tsx).
      let webhookCreated = false;
      if (trigger === "schedule" && cronExpression.trim()) {
        try {
          await createTrigger.mutateAsync({
            agentId: agent.id,
            cronExpression,
            taskText: `Scheduled run for ${identity.name}`,
          });
        } catch (err) {
          toast.error("Agent hired, but the schedule could not be saved", {
            description: String(err),
          });
        }
      } else if (trigger === "webhook") {
        try {
          await createTrigger.mutateAsync({
            agentId: agent.id,
            kind: "webhook",
            taskText: `React to whatever this webhook sends, on ${identity.name}'s behalf.`,
          });
          webhookCreated = true;
        } catch (err) {
          toast.error("Agent hired, but the webhook could not be created", {
            description: String(err),
          });
        }
      }

      toast.success("Agent hired", {
        description: `${identity.name || "New hire"} is ready — ${tools.length} tools, ${model?.name ?? ""}.`,
      });
      if (notInDeptFrame.length > 0) {
        toast.warning("Some tools need department approval first", {
          description: `${notInDeptFrame.join(", ")} — not yet enabled in this department's tool settings, so ${identity.name || "the agent"} cannot use ${notInDeptFrame.length === 1 ? "it" : "them"} yet. Enable it under the department's Settings tab.`,
          duration: 10000,
        });
      }
      reset();
      onOpenChange(false);
      // The webhook URL only exists on the agent's own page (TriggerEditor)
      // -- send the operator straight there to copy it, instead of also
      // rendering it inside this dialog.
      if (webhookCreated) navigate({ to: "/agents/$id", params: { id: agent.id } });
    } catch (err) {
      toast.error("Could not hire agent", { description: String(err) });
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
                <h3 className="font-serif text-2xl leading-none">Hire a new agent</h3>
                <p className="mt-1.5 text-xs text-muted-foreground">
                  Step {step + 1} of {steps.length} · {activeStep.subtitle}
                </p>
              </div>
            </div>
            <button
              onClick={() => onOpenChange(false)}
              className="grid h-8 w-8 shrink-0 place-items-center rounded-md text-muted-foreground transition hover:bg-background/40 hover:text-foreground"
              aria-label="Close"
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
                <li key={s.title} className="flex min-w-0 items-center gap-2">
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
                  {tools.length} tool{tools.length === 1 ? "" : "s"} selected
                </span>
                <button
                  type="button"
                  onClick={() => setPickerOpen(true)}
                  disabled={connections.filter((c) => !tools.includes(c.name)).length === 0}
                  className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background/40 px-2.5 py-1.5 text-xs font-medium text-foreground transition hover:bg-background/70 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <Wrench className="h-3.5 w-3.5" /> Add tool
                </button>
              </div>
              {mcpTools.length === 0 && (
                <p className="rounded-md border border-dashed border-border px-3 py-2 text-xs text-muted-foreground">
                  No MCP connections yet. Add one under Capas to offer it here.
                </p>
              )}
              {tools.length === 0 && mcpTools.length > 0 && (
                <p className="rounded-md border border-dashed border-border px-3 py-2 text-xs text-muted-foreground">
                  No tools selected yet — click "Add tool" to pick one.
                </p>
              )}
              <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3">
                {mcpTools
                  .filter((i) => tools.includes(i.id))
                  .map((i) => {
                    const Icon = i.icon;
                    const inFrame = !!frameTools[i.id]?.enabled;
                    const toolLogins = loginsByKey[i.id] ?? [];
                    const credentialType = connections.find((c) => c.name === i.id)?.credentialType;
                    const pickedCredentialId =
                      toolLogins.find((l) => l.id === connectionId[i.id])?.credentialId ?? "";
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
                            title="Remove tool"
                            className="grid h-6 w-6 place-items-center rounded-md text-muted-foreground transition hover:text-destructive"
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        </div>
                        <div className="w-full min-w-0">
                          <div className="truncate text-sm font-medium">{i.name}</div>
                          <div className="truncate text-[10px] text-muted-foreground">
                            {connectedNames.has(i.id) ? "via MCP" : "not connected"}
                          </div>
                        </div>
                        {credentialType && (
                          <div className="w-full min-w-0 border-t border-border/60 pt-2">
                            <CredentialPicker
                              credentialType={credentialType}
                              value={pickedCredentialId}
                              onChange={(credentialId) =>
                                pinCredential(i.id, credentialType, credentialId)
                              }
                            />
                            {!inFrame && (
                              <p className="mt-1.5 text-[10px] text-muted-foreground">
                                Not yet enabled for this department — the pick is saved, but won't
                                grant access until it is.
                              </p>
                            )}
                          </div>
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
              onAdd={(name) => {
                toggleTool(name);
                setPickerOpen(false);
              }}
              onClose={() => setPickerOpen(false)}
            />
          )}

          {step === 3 && (
            <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,320px)]">
              <div className="space-y-4">
                <Field label="Guardrail label">
                  <input
                    value={guardrail}
                    onChange={(e) => setGuardrail(e.target.value)}
                    placeholder="A short note for the profile card"
                    className="w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
                  />
                  <p className="mt-1 text-[11px] text-muted-foreground">
                    Just a label for this card — it doesn't change what the agent can actually do.
                  </p>
                </Field>
                <Field label="Trigger">
                  <div className="grid gap-2 sm:grid-cols-3">
                    {[
                      { id: "on-demand", label: "On-Demand", icon: Zap, disabled: false },
                      { id: "schedule", label: "Schedule", icon: Clock, disabled: false },
                      { id: "webhook", label: "Webhook", icon: Webhook, disabled: false },
                    ].map((t) => {
                      const Icon = t.icon;
                      const on = trigger === t.id;
                      return (
                        <button
                          key={t.id}
                          type="button"
                          onClick={() => !t.disabled && setTrigger(t.id)}
                          disabled={t.disabled}
                          className={cn(
                            "inline-flex flex-col items-center gap-1.5 rounded-md border px-3 py-3 text-xs transition",
                            on
                              ? "border-primary bg-primary/10 text-primary"
                              : "border-border bg-background/30 text-muted-foreground hover:border-primary/40 hover:text-foreground",
                            t.disabled && "cursor-not-allowed opacity-50 hover:border-border",
                          )}
                        >
                          <Icon className="h-4 w-4" />
                          {t.label}
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
                          toast.success("Schedule set", {
                            description: "Saved once you hire the agent.",
                          })
                        }
                        saveLabel="Looks good"
                      />
                    </div>
                  )}
                  {trigger === "webhook" && (
                    <p className="mt-3 rounded-md border border-dashed border-border px-3 py-2 text-[11px] text-muted-foreground">
                      A unique webhook URL is generated once the agent is hired. You'll land on the
                      agent's page to copy it — Odoo's Automation Rules or any other tool that can
                      send a POST request works.
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
              "Cancel"
            ) : (
              <>
                <ChevronLeft className="h-4 w-4" /> Back
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
              Next <ChevronRight className="h-4 w-4" />
            </button>
          ) : (
            <button
              onClick={finish}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:brightness-110 glow-teal"
            >
              <Check className="h-4 w-4" /> Hire agent
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
  const selected = useMemo(
    () => mcpTools.filter((i) => toolIds.includes(i.id)),
    [mcpTools, toolIds],
  );
  const triggerLabel =
    trigger === "on-demand" ? "On-Demand" : trigger === "schedule" ? "Schedule" : "Webhook";

  return (
    <div className="relative overflow-hidden rounded-2xl border border-primary/40 bg-gradient-to-b from-primary/10 via-panel to-panel p-5 shadow-[0_0_0_1px_var(--primary)]/10">
      <div className="pointer-events-none absolute -right-10 -top-10 h-40 w-40 rounded-full bg-primary/15 blur-3xl" />
      <div className="mb-4 flex items-center justify-between text-[10px] uppercase tracking-widest text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <Sparkles className="h-3 w-3 text-primary" />
          Agent profile
        </span>
        <span className="rounded-full border border-primary/40 bg-primary/10 px-2 py-0.5 text-[9px] text-primary">
          ready to launch
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
          <div className="truncate font-serif text-2xl leading-none">{name || "New agent"}</div>
          <div className="mt-1 truncate text-xs text-muted-foreground">{role || "Role …"}</div>
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
        <Row icon={<Zap className="h-3 w-3" />} label="Trigger">
          <span className="font-medium text-foreground">{triggerLabel}</span>
        </Row>
        <Row icon={<Shield className="h-3 w-3" />} label="Guardrail">
          <span className="text-foreground">{guardrail || "—"}</span>
        </Row>
      </dl>

      <div className="mt-4">
        <div className="mb-1.5 text-[10px] uppercase tracking-widest text-muted-foreground">
          Tools ({selected.length})
        </div>
        {selected.length === 0 ? (
          <div className="rounded-md border border-dashed border-border px-2 py-1.5 text-[11px] text-muted-foreground">
            No tools selected yet.
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
