// Wizard for registering a tenant-owned MCP server (local/stdio or
// remote/URL) or a plain HTTP API (manually described tools) as a real capa.
// Installed through the existing POST /capas -> enable -> setup pipeline --
// no new backend endpoints, no vendor-specific logic anywhere in this file.
import { useState } from "react";
import {
  Check,
  ChevronLeft,
  ChevronRight,
  ClipboardCheck,
  FlaskConical,
  Globe,
  Link2,
  Plus,
  PlugZap,
  Terminal,
  Trash2,
  X,
} from "lucide-react";
import { toast } from "sonner";
import { Field } from "@/components/agent-identity-fields";
import { api } from "@/lib/api";
import { useEnablePlugin, useInstallCustomCapa, useTestMcpConnectionById } from "@/lib/hooks";
import { cn } from "@/lib/utils";

type ServerType = "stdio" | "remote_mcp" | "manual_http";

interface EnvRow {
  key: string;
  value: string;
  secret: boolean;
}

interface HttpToolDraft {
  name: string;
  description: string;
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  urlTemplate: string;
  paramSchema: string; // raw JSON text, validated on submit
}

interface WizardState {
  name: string;
  summary: string;
  serverType: ServerType | null;
  // stdio
  command: string;
  args: string; // space-separated, split on submit
  envRows: EnvRow[];
  // remote_mcp
  serverUrl: string;
  remoteTransport: "http" | "sse";
  authHeaderName: string;
  authHeaderValue: string;
  // manual_http
  baseUrl: string;
  httpTools: HttpToolDraft[];
  // test step (stdio/remote_mcp only)
  testedToolNames: string[] | null;
  testError: string | null;
  // the capa id from a successful useInstallCustomCapa call, so step 4's Save
  // does not re-install if step 3 already installed it (e.g. to run a test).
  installedPluginId: string | null;
}

const INITIAL_STATE: WizardState = {
  name: "",
  summary: "",
  serverType: null,
  command: "",
  args: "",
  envRows: [],
  serverUrl: "",
  remoteTransport: "http",
  authHeaderName: "Authorization",
  authHeaderValue: "",
  baseUrl: "",
  httpTools: [],
  testedToolNames: null,
  testError: null,
  installedPluginId: null,
};

const SERVER_TYPES: Array<{
  id: ServerType;
  title: string;
  description: string;
  icon: typeof Terminal;
}> = [
  {
    id: "stdio",
    title: "Local MCP server",
    description: "Runs on the machine oc8 lives on -- started with a command and arguments.",
    icon: Terminal,
  },
  {
    id: "remote_mcp",
    title: "Remote MCP server",
    description: "Already running somewhere reachable over HTTP or SSE.",
    icon: Globe,
  },
  {
    id: "manual_http",
    title: "Plain HTTP API",
    description: "Not MCP at all -- describe individual REST endpoints as tools by hand.",
    icon: Link2,
  },
];

const HTTP_METHODS: HttpToolDraft["method"][] = ["GET", "POST", "PUT", "PATCH", "DELETE"];

type StepKey = "type" | "connection" | "test" | "summary";

const ALL_STEPS: Array<{ key: StepKey; title: string; subtitle: string; icon: typeof Terminal }> = [
  { key: "type", title: "Name & Type", subtitle: "What are you connecting?", icon: PlugZap },
  {
    key: "connection",
    title: "Connection",
    subtitle: "How does oc8 reach it?",
    icon: PlugZap,
  },
  {
    key: "test",
    title: "Test connection",
    subtitle: "Confirm it actually works.",
    icon: FlaskConical,
  },
  {
    key: "summary",
    title: "Summary & save",
    subtitle: "Review, then install it.",
    icon: ClipboardCheck,
  },
];

async function buildManifest(state: WizardState): Promise<Record<string, unknown>> {
  const fields: Array<Record<string, unknown>> = [];
  const secretEnvFields: Record<string, string> = {};
  const envFields: Record<string, string> = {};
  const plainEnv: Record<string, string> = {};

  if (state.serverType === "stdio") {
    for (const row of state.envRows) {
      if (!row.key) continue;
      if (row.secret) {
        const fieldKey = `env_${row.key}`;
        fields.push({ key: fieldKey, label: row.key, kind: "password" });
        secretEnvFields[row.key] = fieldKey;
      } else {
        plainEnv[row.key] = row.value;
      }
    }
  } else {
    // remote_mcp and manual_http both use one optional auth header.
    if (state.authHeaderName && state.authHeaderValue) {
      fields.push({ key: "auth_header_value", label: state.authHeaderName, kind: "password" });
      secretEnvFields[state.authHeaderName] = "auth_header_value";
    }
  }

  const connectionConfig: Record<string, unknown> =
    state.serverType === "stdio"
      ? {}
      : state.serverType === "remote_mcp"
        ? { auth_header_name: state.authHeaderName || undefined }
        : {
            auth_header_name: state.authHeaderName || undefined,
            http_tools: state.httpTools.map((t) => ({
              name: t.name,
              description: t.description,
              method: t.method,
              url_template: t.urlTemplate,
              param_schema: JSON.parse(t.paramSchema || "{}"),
            })),
          };

  const transport =
    state.serverType === "stdio"
      ? "stdio"
      : state.serverType === "remote_mcp"
        ? state.remoteTransport
        : "manual_http";

  return {
    name: state.name,
    version: "1.0.0",
    type: "tool_pack",
    summary: state.summary,
    tool_pack: {
      connections: [
        {
          key: "default",
          name: state.name,
          server_url:
            state.serverType === "stdio"
              ? ""
              : state.serverType === "remote_mcp"
                ? state.serverUrl
                : state.baseUrl,
          transport,
          config: connectionConfig,
        },
      ],
    },
    setup: {
      title: state.name,
      fields,
      mcp: {
        connection_key: "default",
        name: state.name,
        command: state.serverType === "stdio" ? state.command : "",
        args: state.serverType === "stdio" ? state.args.split(" ").filter(Boolean) : [],
        env_fields: envFields,
        env: plainEnv,
        secret_env_fields: secretEnvFields,
      },
    },
  };
}

/** The setup-form values a saved/tested WizardState resolves to -- the same
 * shape `buildManifest`'s own `fields`/`secret_env_fields` keys expect back. */
function buildSetupValues(state: WizardState): Record<string, string> {
  const values: Record<string, string> = {};
  if (state.serverType === "stdio") {
    for (const row of state.envRows) {
      if (row.secret && row.key) values[`env_${row.key}`] = row.value;
    }
  } else if (state.authHeaderName && state.authHeaderValue) {
    values.auth_header_value = state.authHeaderValue;
  }
  return values;
}

export function CustomMcpWizard({
  open,
  onOpenChange,
  onExported,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  // Called after a successful "Save & export as capa", with the resulting
  // capa's id, so Task 12 can hand it off to CapaExportWizard.
  onExported?: (capaId: string) => void;
}) {
  const [step, setStep] = useState(0);
  const [state, setState] = useState<WizardState>(INITIAL_STATE);
  const [stepError, setStepError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const installCustomCapa = useInstallCustomCapa();
  const enablePlugin = useEnablePlugin();
  const testConnection = useTestMcpConnectionById();

  const steps =
    state.serverType === "manual_http" ? ALL_STEPS.filter((s) => s.key !== "test") : ALL_STEPS;
  const activeStep = steps[step] ?? steps[0];
  const StepIcon = activeStep.icon;
  const progress = ((step + 1) / steps.length) * 100;

  if (!open) return null;

  function reset() {
    setStep(0);
    setState(INITIAL_STATE);
    setStepError(null);
  }

  function close() {
    reset();
    onOpenChange(false);
  }

  function update(patch: Partial<WizardState>) {
    setState((s) => ({ ...s, ...patch }));
  }

  function canProceedFrom(key: StepKey): boolean {
    if (key === "type") return state.name.trim().length > 0 && state.serverType !== null;
    if (key === "connection") {
      if (state.serverType === "stdio") return state.command.trim().length > 0;
      if (state.serverType === "remote_mcp") return state.serverUrl.trim().length > 0;
      if (state.serverType === "manual_http") {
        return state.baseUrl.trim().length > 0 && state.httpTools.length > 0;
      }
      return false;
    }
    return true;
  }

  const canProceed = canProceedFrom(activeStep.key);

  function handleNext() {
    if (activeStep.key === "connection" && state.serverType === "manual_http") {
      for (const tool of state.httpTools) {
        if (!tool.paramSchema.trim()) continue;
        try {
          JSON.parse(tool.paramSchema);
        } catch {
          setStepError(
            `Invalid JSON in the parameter schema for "${tool.name || "unnamed tool"}".`,
          );
          return;
        }
      }
    }
    if (!canProceed) return;
    setStepError(null);
    setStep((s) => Math.min(s + 1, steps.length - 1));
  }

  function handleBack() {
    if (step === 0) {
      close();
      return;
    }
    setStepError(null);
    setStep((s) => Math.max(s - 1, 0));
  }

  // Installs the capa (skipped if step 3 already did so), then always
  // (re-)enables and (re-)configures it -- both idempotent server-side, so
  // running this again in step 4 after step 3 already ran it is harmless.
  // `useConfigurePlugin(pluginId)` takes its pluginId at hook-construction
  // time, which doesn't fit a value only known here at runtime -- calling
  // the same REST endpoint it wraps directly avoids forcing a fixed-argument
  // hook into a dynamic-argument shape (see capa-setup-dialog.tsx for the
  // same enable-then-configure-then-test sequence via hooks, for the case
  // where the plugin id is already known up front).
  async function installEnableConfigure(): Promise<{
    pluginId: string;
    connectionId: string | null;
  }> {
    let pluginId = state.installedPluginId;
    if (!pluginId) {
      const manifest = await buildManifest(state);
      const installed = await installCustomCapa.mutateAsync({ manifest });
      pluginId = installed.pluginId;
      update({ installedPluginId: pluginId });
    }
    await enablePlugin.mutateAsync({ pluginId, grantedPermissions: [] });
    const values = buildSetupValues(state);
    const result = await api.post<{ connectionId: string | null }>(`/capas/${pluginId}/setup`, {
      values,
    });
    return { pluginId, connectionId: result.connectionId };
  }

  async function runTest() {
    setBusy(true);
    update({ testError: null, testedToolNames: null });
    try {
      const { connectionId } = await installEnableConfigure();
      if (!connectionId) {
        update({ testError: "No connection was created to test." });
        return;
      }
      const tested = await testConnection.mutateAsync(connectionId);
      if (tested.connected) {
        const tools = Array.isArray(tested.health?.tools)
          ? (tested.health.tools as unknown[]).filter((x): x is string => typeof x === "string")
          : [];
        update({ testedToolNames: tools, testError: null });
      } else {
        const detail =
          typeof tested.health?.error === "string" ? tested.health.error : "Connection failed.";
        update({ testError: detail, testedToolNames: null });
      }
    } catch (err) {
      update({
        testError: err instanceof Error ? err.message : "Connection test failed.",
        testedToolNames: null,
      });
    } finally {
      setBusy(false);
    }
  }

  async function handleSave(exportAfter: boolean) {
    setBusy(true);
    try {
      const { pluginId } = await installEnableConfigure();
      toast.success(`${state.name} saved.`, {
        description: exportAfter ? "Ready to export." : undefined,
      });
      if (exportAfter) onExported?.(pluginId);
      close();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Could not save this connection.");
    } finally {
      setBusy(false);
    }
  }

  function addEnvRow() {
    update({ envRows: [...state.envRows, { key: "", value: "", secret: false }] });
  }

  function updateEnvRow(index: number, patch: Partial<EnvRow>) {
    update({
      envRows: state.envRows.map((row, i) => (i === index ? { ...row, ...patch } : row)),
    });
  }

  function removeEnvRow(index: number) {
    update({ envRows: state.envRows.filter((_, i) => i !== index) });
  }

  function addHttpTool() {
    update({
      httpTools: [
        ...state.httpTools,
        { name: "", description: "", method: "GET", urlTemplate: "", paramSchema: "" },
      ],
    });
  }

  function updateHttpTool(index: number, patch: Partial<HttpToolDraft>) {
    update({
      httpTools: state.httpTools.map((tool, i) => (i === index ? { ...tool, ...patch } : tool)),
    });
  }

  function removeHttpTool(index: number) {
    update({ httpTools: state.httpTools.filter((_, i) => i !== index) });
  }

  const isLastStep = step === steps.length - 1;

  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur-md animate-in fade-in duration-200"
      onClick={(e) => e.target === e.currentTarget && close()}
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
                <h3 className="font-serif text-2xl leading-none">Connect a custom MCP or API</h3>
                <p className="mt-1.5 text-xs text-muted-foreground">
                  Step {step + 1} of {steps.length} · {activeStep.subtitle}
                </p>
              </div>
            </div>
            <button
              onClick={close}
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
          {activeStep.key === "type" && (
            <div className="space-y-4">
              <Field label="Name">
                <input
                  value={state.name}
                  onChange={(e) => update({ name: e.target.value })}
                  placeholder="e.g. Internal ticketing API"
                  autoFocus
                  className="w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
                />
              </Field>
              <Field label="Summary">
                <input
                  value={state.summary}
                  onChange={(e) => update({ summary: e.target.value })}
                  placeholder="A short description for this capa's card"
                  className="w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
                />
              </Field>
              <Field label="Server type">
                <div className="grid gap-2.5 sm:grid-cols-3">
                  {SERVER_TYPES.map((type) => {
                    const Icon = type.icon;
                    const selected = state.serverType === type.id;
                    return (
                      <button
                        key={type.id}
                        type="button"
                        onClick={() => update({ serverType: type.id })}
                        className={cn(
                          "cursor-pointer rounded-xl border-2 p-4 text-left transition",
                          selected
                            ? "border-primary bg-primary/5"
                            : "border-border bg-background/30 hover:border-primary/40",
                        )}
                      >
                        <Icon
                          className={cn(
                            "h-5 w-5",
                            selected ? "text-primary" : "text-muted-foreground",
                          )}
                        />
                        <div className="mt-2 text-sm font-medium">{type.title}</div>
                        <div className="mt-1 text-[11px] text-muted-foreground">
                          {type.description}
                        </div>
                      </button>
                    );
                  })}
                </div>
              </Field>
            </div>
          )}

          {activeStep.key === "connection" && state.serverType === "stdio" && (
            <div className="space-y-4">
              <Field label="Command">
                <input
                  value={state.command}
                  onChange={(e) => update({ command: e.target.value })}
                  placeholder="npx -y …"
                  className="w-full rounded-md border border-border bg-background/40 px-3 py-2 font-mono text-sm outline-none focus:border-primary/50"
                />
              </Field>
              <Field label="Arguments (space-separated)">
                <input
                  value={state.args}
                  onChange={(e) => update({ args: e.target.value })}
                  className="w-full rounded-md border border-border bg-background/40 px-3 py-2 font-mono text-sm outline-none focus:border-primary/50"
                />
              </Field>
              <EnvRowsEditor
                rows={state.envRows}
                onAdd={addEnvRow}
                onChange={updateEnvRow}
                onRemove={removeEnvRow}
              />
            </div>
          )}

          {activeStep.key === "connection" && state.serverType === "remote_mcp" && (
            <div className="space-y-4">
              <Field label="Server URL">
                <input
                  value={state.serverUrl}
                  onChange={(e) => update({ serverUrl: e.target.value })}
                  placeholder="https://…"
                  className="w-full rounded-md border border-border bg-background/40 px-3 py-2 font-mono text-sm outline-none focus:border-primary/50"
                />
              </Field>
              <Field label="Transport">
                <div className="grid grid-cols-2 gap-2">
                  {(["http", "sse"] as const).map((t) => (
                    <button
                      key={t}
                      type="button"
                      onClick={() => update({ remoteTransport: t })}
                      className={cn(
                        "rounded-md border px-3 py-2 text-xs uppercase tracking-wide transition",
                        state.remoteTransport === t
                          ? "border-primary bg-primary/10 text-primary"
                          : "border-border bg-background/30 text-muted-foreground hover:border-primary/40",
                      )}
                    >
                      {t}
                    </button>
                  ))}
                </div>
              </Field>
              <AuthHeaderFields state={state} onChange={update} />
            </div>
          )}

          {activeStep.key === "connection" && state.serverType === "manual_http" && (
            <div className="space-y-4">
              <Field label="Base URL">
                <input
                  value={state.baseUrl}
                  onChange={(e) => update({ baseUrl: e.target.value })}
                  placeholder="https://api.example.com"
                  className="w-full rounded-md border border-border bg-background/40 px-3 py-2 font-mono text-sm outline-none focus:border-primary/50"
                />
              </Field>
              <AuthHeaderFields state={state} onChange={update} />
              <div>
                <div className="mb-2 flex items-center justify-between">
                  <span className="text-xs uppercase tracking-wider text-muted-foreground">
                    Tools
                  </span>
                  <button
                    type="button"
                    onClick={addHttpTool}
                    className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background/40 px-2.5 py-1.5 text-xs font-medium text-foreground transition hover:bg-background/70"
                  >
                    <Plus className="h-3.5 w-3.5" /> Add tool
                  </button>
                </div>
                {state.httpTools.length === 0 && (
                  <p className="rounded-md border border-dashed border-border px-3 py-2 text-xs text-muted-foreground">
                    No endpoints described yet — add at least one to continue.
                  </p>
                )}
                <div className="space-y-3">
                  {state.httpTools.map((tool, i) => (
                    <div
                      key={i}
                      className="space-y-2 rounded-lg border border-border bg-background/20 p-3"
                    >
                      <div className="flex items-center justify-between">
                        <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
                          Tool {i + 1}
                        </span>
                        <button
                          type="button"
                          onClick={() => removeHttpTool(i)}
                          title="Remove tool"
                          className="grid h-6 w-6 place-items-center rounded-md text-muted-foreground transition hover:text-destructive"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </div>
                      <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_100px]">
                        <input
                          value={tool.name}
                          onChange={(e) => updateHttpTool(i, { name: e.target.value })}
                          placeholder="Tool name"
                          className="w-full rounded-md border border-border bg-background/40 px-3 py-1.5 text-sm outline-none focus:border-primary/50"
                        />
                        <select
                          value={tool.method}
                          onChange={(e) =>
                            updateHttpTool(i, {
                              method: e.target.value as HttpToolDraft["method"],
                            })
                          }
                          className="w-full rounded-md border border-border bg-background/40 px-2 py-1.5 text-sm outline-none focus:border-primary/50"
                        >
                          {HTTP_METHODS.map((m) => (
                            <option key={m} value={m}>
                              {m}
                            </option>
                          ))}
                        </select>
                      </div>
                      <input
                        value={tool.description}
                        onChange={(e) => updateHttpTool(i, { description: e.target.value })}
                        placeholder="Description"
                        className="w-full rounded-md border border-border bg-background/40 px-3 py-1.5 text-sm outline-none focus:border-primary/50"
                      />
                      <input
                        value={tool.urlTemplate}
                        onChange={(e) => updateHttpTool(i, { urlTemplate: e.target.value })}
                        placeholder="/orders/{order_id}"
                        className="w-full rounded-md border border-border bg-background/40 px-3 py-1.5 font-mono text-sm outline-none focus:border-primary/50"
                      />
                      <textarea
                        value={tool.paramSchema}
                        onChange={(e) => updateHttpTool(i, { paramSchema: e.target.value })}
                        placeholder='{"type": "object", "properties": {…}}'
                        rows={3}
                        className="w-full rounded-md border border-border bg-background/40 px-3 py-1.5 font-mono text-xs outline-none focus:border-primary/50"
                      />
                    </div>
                  ))}
                </div>
              </div>
              {stepError && <p className="text-xs text-[color:var(--status-error)]">{stepError}</p>}
            </div>
          )}

          {activeStep.key === "test" && (
            <div className="space-y-4">
              <p className="text-xs text-muted-foreground">
                This installs and configures the capa now, then pings the server and asks it which
                tools it offers -- the same test a "Test connection" button anywhere else in oc8
                runs.
              </p>
              <button
                type="button"
                onClick={runTest}
                disabled={busy}
                className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background/40 px-3 py-2 text-sm font-medium transition hover:bg-background/70 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <FlaskConical className="h-4 w-4" />
                {busy ? "Testing…" : "Test connection"}
              </button>
              {state.testedToolNames && (
                <div className="rounded-md border border-border bg-background/20 p-3">
                  <p className="text-xs font-medium text-[color:var(--status-running)]">
                    Connected — {state.testedToolNames.length} tool
                    {state.testedToolNames.length === 1 ? "" : "s"} discovered.
                  </p>
                  {state.testedToolNames.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-1">
                      {state.testedToolNames.map((name) => (
                        <span
                          key={name}
                          className="rounded-full border border-border bg-background/30 px-2 py-0.5 text-[10px] text-muted-foreground"
                        >
                          {name}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )}
              {state.testError && (
                <p className="text-xs text-[color:var(--status-error)]">{state.testError}</p>
              )}
            </div>
          )}

          {activeStep.key === "summary" && <SummaryCard state={state} />}
        </div>

        <footer className="flex items-center justify-between gap-3 border-t border-border bg-background/30 px-6 py-4">
          <button
            onClick={handleBack}
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
          {!isLastStep ? (
            <button
              onClick={handleNext}
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
            <div className="flex items-center gap-2">
              <button
                onClick={() => handleSave(false)}
                disabled={busy}
                className="inline-flex items-center gap-1.5 rounded-md border border-border px-4 py-2 text-sm font-medium text-foreground transition hover:bg-background/40 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Save
              </button>
              <button
                onClick={() => handleSave(true)}
                disabled={busy}
                className="inline-flex items-center gap-1.5 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:brightness-110 glow-teal disabled:cursor-not-allowed disabled:opacity-50"
              >
                <Check className="h-4 w-4" /> Save & export as capa
              </button>
            </div>
          )}
        </footer>
      </div>
    </div>
  );
}

function EnvRowsEditor({
  rows,
  onAdd,
  onChange,
  onRemove,
}: {
  rows: EnvRow[];
  onAdd: () => void;
  onChange: (index: number, patch: Partial<EnvRow>) => void;
  onRemove: (index: number) => void;
}) {
  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs uppercase tracking-wider text-muted-foreground">
          Environment variables
        </span>
        <button
          type="button"
          onClick={onAdd}
          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background/40 px-2.5 py-1.5 text-xs font-medium text-foreground transition hover:bg-background/70"
        >
          <Plus className="h-3.5 w-3.5" /> Add row
        </button>
      </div>
      {rows.length === 0 && (
        <p className="rounded-md border border-dashed border-border px-3 py-2 text-xs text-muted-foreground">
          No environment variables yet.
        </p>
      )}
      <div className="space-y-2">
        {rows.map((row, i) => (
          <div key={i} className="flex items-center gap-2">
            <input
              value={row.key}
              onChange={(e) => onChange(i, { key: e.target.value })}
              placeholder="KEY"
              className="w-1/3 rounded-md border border-border bg-background/40 px-2.5 py-1.5 font-mono text-xs outline-none focus:border-primary/50"
            />
            <input
              value={row.value}
              onChange={(e) => onChange(i, { value: e.target.value })}
              type={row.secret ? "password" : "text"}
              placeholder="value"
              className="flex-1 rounded-md border border-border bg-background/40 px-2.5 py-1.5 font-mono text-xs outline-none focus:border-primary/50"
            />
            <label className="flex shrink-0 items-center gap-1.5 text-[11px] text-muted-foreground">
              <input
                type="checkbox"
                checked={row.secret}
                onChange={(e) => onChange(i, { secret: e.target.checked })}
              />
              secret
            </label>
            <button
              type="button"
              onClick={() => onRemove(i)}
              title="Remove"
              className="grid h-7 w-7 shrink-0 place-items-center rounded-md text-muted-foreground transition hover:text-destructive"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

function AuthHeaderFields({
  state,
  onChange,
}: {
  state: WizardState;
  onChange: (patch: Partial<WizardState>) => void;
}) {
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      <Field label="Auth header name">
        <input
          value={state.authHeaderName}
          onChange={(e) => onChange({ authHeaderName: e.target.value })}
          placeholder="Authorization"
          className="w-full rounded-md border border-border bg-background/40 px-3 py-2 font-mono text-sm outline-none focus:border-primary/50"
        />
      </Field>
      <Field label="Auth header value">
        <input
          value={state.authHeaderValue}
          onChange={(e) => onChange({ authHeaderValue: e.target.value })}
          type="password"
          placeholder="Bearer …"
          className="w-full rounded-md border border-border bg-background/40 px-3 py-2 font-mono text-sm outline-none focus:border-primary/50"
        />
      </Field>
    </div>
  );
}

const SERVER_TYPE_LABELS: Record<ServerType, string> = {
  stdio: "Local MCP server",
  remote_mcp: "Remote MCP server",
  manual_http: "Plain HTTP API",
};

function SummaryCard({ state }: { state: WizardState }) {
  const typeLabel = state.serverType ? SERVER_TYPE_LABELS[state.serverType] : "—";
  const toolOrEndpointCount =
    state.serverType === "manual_http"
      ? state.httpTools.length
      : (state.testedToolNames?.length ?? null);
  const secretCount =
    state.serverType === "stdio"
      ? state.envRows.filter((r) => r.secret && r.key).length
      : state.authHeaderName && state.authHeaderValue
        ? 1
        : 0;

  return (
    <div className="relative overflow-hidden rounded-2xl border border-primary/40 bg-gradient-to-b from-primary/10 via-panel to-panel p-5 shadow-[0_0_0_1px_var(--primary)]/10">
      <div className="pointer-events-none absolute -right-10 -top-10 h-40 w-40 rounded-full bg-primary/15 blur-3xl" />
      <div className="mb-4 flex items-center justify-between text-[10px] uppercase tracking-widest text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <ClipboardCheck className="h-3 w-3 text-primary" />
          Capa summary
        </span>
        <span className="rounded-full border border-primary/40 bg-primary/10 px-2 py-0.5 text-[9px] text-primary">
          ready to save
        </span>
      </div>

      <div className="min-w-0">
        <div className="truncate font-serif text-2xl leading-none">{state.name || "New capa"}</div>
        {state.summary && (
          <p className="mt-2 line-clamp-3 border-l-2 border-primary/40 pl-3 text-xs italic text-muted-foreground">
            "{state.summary}"
          </p>
        )}
      </div>

      <dl className="mt-4 space-y-2.5 text-xs">
        <Row icon={<PlugZap className="h-3 w-3" />} label="Type">
          <span className="font-medium text-foreground">{typeLabel}</span>
        </Row>
        <Row icon={<FlaskConical className="h-3 w-3" />} label="Tools">
          <span className="font-medium text-foreground">
            {toolOrEndpointCount === null ? "not tested yet" : toolOrEndpointCount}
          </span>
        </Row>
        <Row icon={<Check className="h-3 w-3" />} label="Secrets">
          <span className="font-medium text-foreground">{secretCount}</span>
        </Row>
      </dl>
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
