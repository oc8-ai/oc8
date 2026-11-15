// Wizard for registering a tenant-owned MCP server (local/stdio or
// remote/URL) or a plain HTTP API (tools described by hand, or by a custom
// server source an edition registers) as a real capa.
// Installed through the existing POST /capas -> enable -> setup pipeline (an
// HTTP API's tools edited after a test are replaced in place through
// PATCH /mcp/connections/{id}/http-tools) -- no vendor-specific logic
// anywhere in this file.
import { useRef, useState } from "react";
import {
  Check,
  ChevronLeft,
  ChevronRight,
  ClipboardCheck,
  FlaskConical,
  Globe,
  Link2,
  Pencil,
  Plus,
  PlugZap,
  Terminal,
  Trash2,
  Users,
  X,
} from "lucide-react";
import { toast } from "sonner";
import { Field } from "@/components/agent-identity-fields";
import { HttpLoginFields } from "@/components/http-login-fields";
import { HttpTestCallPanel, type TestableTool } from "@/components/http-test-call-panel";
import { McpTestLogDrawer } from "@/components/mcp-test-log-drawer";
import { useCustomServerSources } from "@/edition/composition";
import { useConfirm } from "@/hooks/use-confirm";
import { api } from "@/lib/api";
import {
  useDepartments,
  useDisablePlugin,
  useEnablePlugin,
  useInstallCustomCapa,
  useTestMcpConnectionById,
} from "@/lib/hooks";
import {
  LOGIN_SECRET_ENV,
  LOGIN_SECRET_FIELD,
  NO_LOGIN,
  loginComplete,
  loginSecret,
  httpToolsUpdateBetween,
  loginToConfig,
  metadataToConfig,
  customServerDraftProblem,
  patchHttpTools,
  postHttpTestCall,
  toolToConfig,
  validateHttpTools,
  type CustomServerDraft,
  httpToolProblemText,
  type HttpToolProblem,
  type HttpLoginDraft,
} from "@/lib/http-tools";
import { useT } from "@/lib/i18n";
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
  // The department whose agents get the connection's tools. The MCP gateway
  // hands a run only its own department's connections, so one left in no
  // department reaches no agent at all.
  departmentId: string;
  // stdio
  command: string;
  args: string; // space-separated, split on submit
  envRows: EnvRow[];
  // remote_mcp
  serverUrl: string;
  authHeaderName: string;
  authHeaderValue: string;
  // manual_http
  baseUrl: string;
  login: HttpLoginDraft;
  httpTools: HttpToolDraft[];
  // The custom server source chosen to describe the endpoints (null: by
  // hand), and what it handed back.
  sourceId: string | null;
  draft: CustomServerDraft | null;
  // test step (stdio/remote_mcp: tools discovered)
  testedToolNames: string[] | null;
  testError: string | null;
  // the connection a test run is/was against -- known only once
  // installEnableConfigure resolves, and set BEFORE the test mutation fires
  // so McpTestLogDrawer is already mounted (with the right cache key) when
  // the backend starts publishing spawn/handshake/list_tools events for it.
  testedConnectionId: string | null;
  // the capa id from a successful useInstallCustomCapa call, so step 4's Save
  // does not re-install if step 3 already installed it (e.g. to run a test).
  installedPluginId: string | null;
  // What that install was made from, so an edit after a test (Back, change a
  // tool, the URL or the login) reaches the connection instead of being
  // silently lost: the manifest it was built from, how many versions this
  // wizard installed, the connection its setup returned and the setup values
  // that connection was last configured with.
  installedManifest: Record<string, unknown> | null;
  installedRevision: number;
  installedConnectionId: string | null;
  installedSetupKey: string | null;
  // Capas installed under earlier names whose disabling has not succeeded
  // yet; the next install, test or save tries again.
  replacedPluginIds: string[];
  // Connections of a department the person moved away from whose removal
  // failed; the next test or save tries again.
  pendingConnectionDeletes: string[];
}

const INITIAL_STATE: WizardState = {
  name: "",
  summary: "",
  serverType: null,
  departmentId: "",
  command: "",
  args: "",
  envRows: [],
  serverUrl: "",
  authHeaderName: "Authorization",
  authHeaderValue: "",
  baseUrl: "",
  login: NO_LOGIN,
  httpTools: [],
  sourceId: null,
  draft: null,
  testedToolNames: null,
  testError: null,
  testedConnectionId: null,
  installedPluginId: null,
  installedManifest: null,
  installedRevision: 0,
  installedConnectionId: null,
  installedSetupKey: null,
  replacedPluginIds: [],
  pendingConnectionDeletes: [],
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
    description: "Already running somewhere reachable over HTTP.",
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

// Tailwind class names can't be built via template literals (e.g. `grid-cols-${n}`)
// -- they have to appear as static strings for the build to extract them.
// Every server type has four steps today (manual_http's test step makes one
// real call of a declared tool); 3 stays covered should a type drop one again.
const STEP_DOT_GRID_COLS: Record<number, string> = {
  3: "grid-cols-3",
  4: "grid-cols-4",
};

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

// The setup field (and `mcp.department_field`) that places the connection in a
// department -- the same declarative mechanism a tool pack's own manifest uses
// (e.g. github_mcp's setup/mcp.toml), so the generic setup form can move it
// later too.
const DEPARTMENT_FIELD = "department";

/** What a source handed back, when the endpoints come from one. */
function draftOf(state: WizardState): CustomServerDraft | null {
  return state.sourceId ? state.draft : null;
}

function loginOf(state: WizardState): HttpLoginDraft {
  return draftOf(state)?.login ?? state.login;
}

async function buildManifest(
  state: WizardState,
  departmentRequired: boolean,
): Promise<Record<string, unknown>> {
  const fields: Array<Record<string, unknown>> = [
    {
      key: DEPARTMENT_FIELD,
      label: "Department",
      kind: "department",
      required: departmentRequired,
    },
  ];
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
  } else if (state.serverType === "remote_mcp") {
    if (state.authHeaderName && state.authHeaderValue) {
      fields.push({ key: "auth_header_value", label: state.authHeaderName, kind: "password" });
      secretEnvFields[state.authHeaderName] = "auth_header_value";
    }
  } else if (loginOf(state).kind !== "none") {
    fields.push({ key: LOGIN_SECRET_FIELD, label: "Secret", kind: "password" });
    secretEnvFields[LOGIN_SECRET_ENV] = LOGIN_SECRET_FIELD;
  }

  const draft = draftOf(state);
  const connectionConfig: Record<string, unknown> =
    state.serverType === "stdio"
      ? {}
      : state.serverType === "remote_mcp"
        ? { auth_header_name: state.authHeaderName || undefined }
        : draft
          ? {
              ...(loginToConfig(draft.login) ? { auth: loginToConfig(draft.login) } : {}),
              http_tools: draft.httpTools.map(toolToConfig),
              ...(draft.openapi ? { openapi: metadataToConfig(draft.openapi) } : {}),
            }
          : {
              ...(loginToConfig(state.login) ? { auth: loginToConfig(state.login) } : {}),
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
        ? "http"
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
                : (draft?.baseUrl ?? state.baseUrl),
          transport,
          ...(draft
            ? { scopes: { read: [...draft.scopes.read], modify: [...draft.scopes.modify] } }
            : {}),
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
        department_field: DEPARTMENT_FIELD,
      },
    },
  };
}

/** The setup-form values a saved/tested WizardState resolves to -- the same
 * shape `buildManifest`'s own `fields`/`secret_env_fields` keys expect back. */
function buildSetupValues(state: WizardState, departmentId: string): Record<string, string> {
  const values: Record<string, string> = {};
  if (departmentId) values[DEPARTMENT_FIELD] = departmentId;
  if (state.serverType === "stdio") {
    for (const row of state.envRows) {
      if (row.secret && row.key) values[`env_${row.key}`] = row.value;
    }
  } else if (state.serverType === "remote_mcp") {
    if (state.authHeaderName && state.authHeaderValue)
      values.auth_header_value = state.authHeaderValue;
  } else if (loginSecret(loginOf(state))) {
    values[LOGIN_SECRET_FIELD] = loginSecret(loginOf(state));
  }
  return values;
}

/** A refusal saying the connection an edit targets is not there any more. */
function connectionGone(err: unknown): boolean {
  const status =
    typeof err === "object" && err !== null && "status" in err
      ? (err as { status: unknown }).status
      : null;
  return status === 404 || status === 409;
}

export function CustomMcpWizard({
  open,
  onOpenChange,
  onExported,
  defaultDepartmentId,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  // Called after a successful "Save & export as capa", with the resulting
  // capa's id, so Task 12 can hand it off to CapaExportWizard.
  onExported?: (capaId: string) => void;
  // Preselected department, when opened from a department's own page.
  defaultDepartmentId?: string;
}) {
  const t = useT();
  const initialState: WizardState = { ...INITIAL_STATE, departmentId: defaultDepartmentId ?? "" };
  const [step, setStep] = useState(0);
  const [state, setState] = useState<WizardState>(initialState);
  const { data: departmentsPage } = useDepartments({ pageSize: 200 });
  const departments = departmentsPage?.items ?? [];
  // Required as soon as the organisation has departments. A preselection that
  // is not (or no longer) one of them counts as none; a single department is
  // the only possible answer.
  const departmentId = departments.some((d) => d.id === state.departmentId)
    ? state.departmentId
    : departments.length === 1
      ? departments[0].id
      : "";
  const departmentName = departments.find((d) => d.id === departmentId)?.name ?? null;
  const [stepError, setStepError] = useState<string | null>(null);
  // What is wrong with each tool, shown under it after a refused Next.
  const [toolProblems, setToolProblems] = useState<Array<HttpToolProblem | null>>([]);

  const toolProblemText = (problem: HttpToolProblem) => httpToolProblemText(problem, t);
  const [busy, setBusy] = useState(false);
  const closing = useRef(false);

  const installCustomCapa = useInstallCustomCapa();
  const enablePlugin = useEnablePlugin();
  const testConnection = useTestMcpConnectionById();
  const disablePlugin = useDisablePlugin();
  // "Test connection" (step 3) genuinely installs+enables+configures a real
  // capa against the backend -- there's no separate sandbox/dry-run path.
  // If the wizard is then cancelled instead of saved, that capa would
  // otherwise sit there enabled with nothing telling the user it happened.
  // There is no delete/uninstall-capa endpoint anywhere in this codebase
  // (checked routes/capas.tsx, lib/hooks.ts, and the backend's capas router
  // -- only install/enable/disable/setup exist, and routes/capas.tsx's own
  // comments confirm "disabled" is the closest analog to removal a capa
  // has, there being no soft-delete column for it), so `requestClose` reuses
  // that existing, non-destructive `useDisablePlugin` -- the same one
  // routes/capas.tsx's own "Disable" action calls -- rather than inventing
  // new backend surface.
  const { confirm, ConfirmDialog } = useConfirm();

  const steps = ALL_STEPS;
  const activeStep = steps[step] ?? steps[0];
  const sources = useCustomServerSources();
  const activeSource =
    state.serverType === "manual_http" && state.sourceId
      ? (sources.find((s) => s.id === state.sourceId) ?? null)
      : null;
  // The source renders its own step (and its own "continue") in a wider dialog.
  const sourceStepActive = activeSource !== null && activeStep.key === "connection";
  // A source that is no longer offered (the caller lacks its capability) does
  // not describe the endpoints: what was typed by hand does.
  const described: WizardState = activeSource ? state : { ...state, sourceId: null };
  const byHandStepActive =
    activeStep.key === "connection" && state.serverType === "manual_http" && !activeSource;
  const StepIcon = activeStep.icon;
  const progress = ((step + 1) / steps.length) * 100;

  if (!open) return null;

  function reset() {
    setStep(0);
    setState(initialState);
    setStepError(null);
  }

  function close() {
    reset();
    onOpenChange(false);
  }

  // Cancel/close entry point for every path that is NOT a completed Save
  // (backdrop click, the header X, and Back from step 1). If step 3 already
  // installed+enabled a real capa via "Test connection", closing without
  // saving would otherwise silently leave it live. Ask first, and reuse the
  // codebase's existing (non-destructive) disable action if the user wants
  // it turned back off -- see the comment by `useDisablePlugin` above for
  // why there's nothing stronger to call.
  async function requestClose() {
    if (busy || closing.current) return;
    if (!state.installedPluginId) {
      close();
      return;
    }
    // Held from the confirm on: a second click (the X, the backdrop, Cancel)
    // must not ask again or run the disable and the retry twice.
    closing.current = true;
    try {
      await disableAndClose();
    } finally {
      closing.current = false;
      setBusy(false);
    }
  }

  async function disableAndClose() {
    if (!state.installedPluginId) return;
    const ok = await confirm({
      title: "Cancel and leave this capa installed?",
      description: `Testing the connection already installed and enabled "${state.name || "this capa"}" in your workspace -- that's not undone by closing this dialog. You can disable it now so it isn't left running unnoticed, or go back and finish saving it.`,
      confirmLabel: "Disable & cancel",
      cancelLabel: "Keep editing",
    });
    if (!ok) return;
    setBusy(true);
    try {
      await disablePlugin.mutateAsync({
        pluginId: state.installedPluginId,
        reason: "Cancelled from the custom MCP wizard after a connection test",
      });
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : "Could not disable this capa -- it's still enabled; disable it from the Capas page.",
      );
    } finally {
      await disableReplacedOnce();
      await removePendingConnectionsOnce();
      close();
    }
  }

  // A capa installed under an earlier name whose disabling failed is
  // otherwise retried on the next test or save; closing means there is
  // none, so try once more and say so if it is still enabled.
  async function disableReplacedOnce() {
    let failed = false;
    for (const id of state.replacedPluginIds) {
      if (id === state.installedPluginId) continue;
      try {
        await disablePlugin.mutateAsync({
          pluginId: id,
          reason: "Replaced in the custom MCP wizard after a rename",
        });
      } catch {
        failed = true;
      }
    }
    if (failed) {
      toast.warning(
        t(
          "A capa installed under an earlier name could not be disabled. Disable it on the Capas page.",
          "Eine Capa, die unter einem früheren Namen installiert wurde, konnte nicht deaktiviert werden. Deaktiviere sie auf der Seite Capas.",
        ),
      );
    }
  }

  // A connection left in a department the person moved away from, whose
  // removal failed, is otherwise retried on the next test or save; closing
  // means there is none, so try once more and say so if it still stays.
  async function removePendingConnectionsOnce() {
    const failed: string[] = [];
    for (const id of state.pendingConnectionDeletes) {
      try {
        await api.delete(`/mcp/connections/${id}`);
      } catch {
        failed.push(id);
      }
    }
    if (failed.length > 0) {
      toast.warning(
        t(
          "The connection in the department you moved away from could not be removed. Delete it from the connections list.",
          "Die Verbindung in der Abteilung, aus der du gewechselt bist, konnte nicht entfernt werden. Lösche sie in der Liste der Verbindungen.",
        ),
      );
    }
  }

  function update(patch: Partial<WizardState>) {
    setState((s) => ({ ...s, ...patch }));
  }

  function canProceedFrom(key: StepKey): boolean {
    if (key === "type") {
      return (
        state.name.trim().length > 0 &&
        state.serverType !== null &&
        (departments.length === 0 || departmentId !== "")
      );
    }
    if (key === "connection") {
      if (state.serverType === "stdio") return state.command.trim().length > 0;
      if (state.serverType === "remote_mcp") return state.serverUrl.trim().length > 0;
      if (state.serverType === "manual_http") {
        if (activeSource) return state.draft !== null;
        return (
          state.baseUrl.trim().length > 0 &&
          state.httpTools.length > 0 &&
          loginComplete(state.login)
        );
      }
      return false;
    }
    return true;
  }

  const canProceed = canProceedFrom(activeStep.key);

  function handleNext() {
    if (activeStep.key === "connection" && state.serverType === "manual_http") {
      const problems = validateHttpTools(state.httpTools);
      if (problems.some((problem) => problem !== null)) {
        setToolProblems(problems);
        return;
      }
      setToolProblems([]);
    }
    if (!canProceed) return;
    setStepError(null);
    setStep((s) => Math.min(s + 1, steps.length - 1));
  }

  function handleBack() {
    if (step === 0) {
      void requestClose();
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
  //
  // When the manifest changed since this wizard installed it (the person went
  // Back after a test and edited something), an edit of the HTTP API's tools
  // alone replaces them on the installed connection and in its manifest
  // (PATCH .../http-tools), with no new install. Anything else -- the name,
  // the URL, the login -- is installed again as a new version, and the
  // connection the old one set up is removed first: enabling only refreshes a
  // connection's declared seams, so the new URL and login reach a connection
  // only when it is made afresh.
  async function installEnableConfigure(): Promise<{
    pluginId: string;
    connectionId: string | null;
  }> {
    const manifest = await buildManifest(described, departments.length > 0);
    const values = buildSetupValues(described, departmentId);
    const setupKey = JSON.stringify(values);
    let pluginId = state.installedPluginId;
    let revision = state.installedRevision;
    // Tracked locally from here on: `update` only reaches `state` after this run.
    let installedConnectionId = state.installedConnectionId;
    const changed =
      state.installedManifest !== null &&
      JSON.stringify(manifest) !== JSON.stringify(state.installedManifest);
    const toolsOnly =
      pluginId && changed && installedConnectionId && state.installedManifest
        ? httpToolsUpdateBetween(state.installedManifest, manifest)
        : null;

    // Removes connections left behind in a department the person moved away
    // from. One that cannot be removed now stays queued for the next test or
    // save -- the setup that replaced it already worked -- with a warning.
    async function removeLeftConnections(extra: string[]): Promise<void> {
      const queued = [...state.pendingConnectionDeletes, ...extra].filter(
        (id, i, all) => id !== installedConnectionId && all.indexOf(id) === i,
      );
      const failed: string[] = [];
      for (const id of queued) {
        try {
          await api.delete(`/mcp/connections/${id}`);
        } catch {
          failed.push(id);
        }
      }
      update({ pendingConnectionDeletes: failed });
      if (failed.length > 0) {
        toast.warning(
          t(
            "The connection in the department you moved away from could not be removed yet. It is tried again on the next test or save.",
            "Die Verbindung in der Abteilung, aus der du gewechselt bist, konnte noch nicht entfernt werden. Beim nächsten Test oder Speichern wird es erneut versucht.",
          ),
        );
      }
    }

    // Setup makes one connection per department, so a changed department
    // yields a new connection; the one the first department had -- with the
    // login -- must not stay reachable to that department's agents.
    async function setUp(id: string): Promise<string | null> {
      const result = await api.post<{ connectionId: string | null }>(`/capas/${id}/setup`, {
        values,
      });
      const before = installedConnectionId;
      installedConnectionId = result.connectionId;
      update({ installedConnectionId: result.connectionId, installedSetupKey: setupKey });
      await removeLeftConnections(before && before !== result.connectionId ? [before] : []);
      return result.connectionId;
    }

    if (pluginId && toolsOnly && installedConnectionId) {
      const connectionId = installedConnectionId;
      let patched = true;
      try {
        await patchHttpTools(connectionId, toolsOnly);
      } catch (err) {
        // The connection was deleted (404) or no longer belongs to this
        // capa's manifest (409): install again, as for any other change.
        if (!connectionGone(err)) throw err;
        patched = false;
        installedConnectionId = null;
        update({ installedConnectionId: null });
      }
      if (patched) {
        update({ installedManifest: manifest });
        // A changed secret or department travels through setup, as always.
        if (setupKey === state.installedSetupKey) {
          await removeLeftConnections([]);
          return { pluginId, connectionId };
        }
        return { pluginId, connectionId: await setUp(pluginId) };
      }
    }

    let pending = state.replacedPluginIds;
    if (!pluginId || changed) {
      if (pluginId && installedConnectionId) {
        await api.delete(`/mcp/connections/${installedConnectionId}`);
        installedConnectionId = null;
        update({ installedConnectionId: null });
      }
      const previous = pluginId;
      revision = previous ? revision + 1 : 0;
      const installed = await installCustomCapa.mutateAsync({
        manifest: { ...manifest, version: `1.0.${revision}` },
      });
      pluginId = installed.pluginId;
      // A new name is a new capa: the one installed under the old name must
      // not stay enabled behind the person's back.
      if (previous && previous !== pluginId && !pending.includes(previous)) {
        pending = [...pending, previous];
      }
      // Recorded before anything else can fail, so a retry does not install
      // this same version again.
      update({
        installedPluginId: pluginId,
        installedManifest: manifest,
        installedRevision: revision,
        replacedPluginIds: pending,
      });
    }
    // Renamed back to an earlier name: that capa is the current one again.
    const current = pluginId;
    pending = pending.filter((id) => id !== current);
    update({ replacedPluginIds: pending });
    for (const replaced of pending) {
      await disablePlugin.mutateAsync({
        pluginId: replaced,
        reason: "Replaced in the custom MCP wizard after a rename",
      });
      pending = pending.filter((id) => id !== replaced);
      update({ replacedPluginIds: pending });
    }
    await enablePlugin.mutateAsync({ pluginId, grantedPermissions: [] });
    return { pluginId, connectionId: await setUp(pluginId) };
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
      update({ testedConnectionId: connectionId });
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

  const sourceDraft = draftOf(described);
  const toolsForTest: TestableTool[] = sourceDraft
    ? sourceDraft.httpTools.map((tool) => ({
        name: tool.name,
        method: tool.method,
        paramSchema: tool.paramSchema,
      }))
    : state.httpTools
        .filter((tool) => tool.name.trim() !== "")
        .map((tool) => {
          let schema: Record<string, unknown> = {};
          try {
            const parsed: unknown = JSON.parse(tool.paramSchema || "{}");
            if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
              schema = parsed as Record<string, unknown>;
            }
          } catch {
            schema = {};
          }
          return { name: tool.name, method: tool.method, paramSchema: schema };
        });

  // What a source handed back is checked by the same rules the server and the
  // hand-written tools use (base URL, login, tools, read/modify), before it
  // can be tested or saved.
  function acceptDraft(draft: CustomServerDraft) {
    const problem = customServerDraftProblem(draft, t, { withConnection: true });
    if (problem) {
      setStepError(problem);
      return;
    }
    update({ draft });
    setStepError(null);
    setStep((s) => Math.min(s + 1, steps.length - 1));
  }

  // The plain HTTP API's test step: install, enable and configure the capa
  // like the MCP test does, then make one real call of the chosen tool.
  async function runManualTestCall(tool: string, args: Record<string, unknown>) {
    setBusy(true);
    try {
      const { connectionId } = await installEnableConfigure();
      if (!connectionId) {
        throw new Error(
          t("No connection was created to test.", "Es wurde keine Verbindung zum Testen angelegt."),
        );
      }
      update({ testedConnectionId: connectionId });
      return await postHttpTestCall(connectionId, tool, args);
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
    setToolProblems((problems) => problems.map((p, i) => (i === index ? null : p)));
    update({
      httpTools: state.httpTools.map((tool, i) => (i === index ? { ...tool, ...patch } : tool)),
    });
  }

  function removeHttpTool(index: number) {
    setToolProblems([]);
    update({ httpTools: state.httpTools.filter((_, i) => i !== index) });
  }

  const isLastStep = step === steps.length - 1;

  return (
    <>
      {ConfirmDialog}
      <div
        className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur-md animate-in fade-in duration-200"
        onClick={(e) => {
          if (e.target === e.currentTarget) void requestClose();
        }}
      >
        <div
          className={cn(
            "w-full overflow-hidden rounded-2xl border border-border bg-panel shadow-2xl animate-in fade-in zoom-in-95 duration-200",
            sourceStepActive ? "max-w-6xl" : "max-w-3xl",
          )}
        >
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
                onClick={() => void requestClose()}
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
              <ol
                className={cn("mt-3 grid gap-2", STEP_DOT_GRID_COLS[steps.length] ?? "grid-cols-4")}
              >
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
            className={cn(
              "space-y-4 overflow-y-auto px-6 py-6 animate-in fade-in slide-in-from-right-2 duration-300",
              sourceStepActive ? "max-h-[78vh]" : "max-h-[62vh]",
            )}
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
                {departments.length > 0 && (
                  <Field label={t("Department", "Abteilung")}>
                    <select
                      aria-label={t("Department", "Abteilung")}
                      value={departmentId}
                      onChange={(e) => update({ departmentId: e.target.value })}
                      // Fixed once a test installed it: a different department
                      // would get a second, untested connection of its own.
                      disabled={state.installedPluginId !== null}
                      className="w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50 disabled:opacity-60"
                    >
                      <option value="">{t("Choose a department", "Abteilung wählen")}</option>
                      {departments.map((d) => (
                        <option key={d.id} value={d.id}>
                          {d.name}
                        </option>
                      ))}
                    </select>
                    <span className="mt-1 block text-[11px] normal-case tracking-normal text-muted-foreground">
                      {t(
                        "Agents in this department get its tools; agents in other departments don't.",
                        "Agents in dieser Abteilung bekommen seine Tools, Agents in anderen Abteilungen nicht.",
                      )}
                    </span>
                  </Field>
                )}
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
                {state.serverType === "manual_http" && sources.length > 0 && (
                  <div
                    role="group"
                    aria-label={t(
                      "How do you describe the endpoints?",
                      "Wie beschreibst du die Endpunkte?",
                    )}
                  >
                    <div className="mb-1.5 text-xs uppercase tracking-wider text-muted-foreground">
                      {t("How do you describe the endpoints?", "Wie beschreibst du die Endpunkte?")}
                    </div>
                    <div className="space-y-2">
                      {[
                        ...sources.map((s) => ({
                          id: s.id as string | null,
                          title: t(s.label.en, s.label.de),
                          description: t(s.description.en, s.description.de),
                          Icon: s.icon,
                        })),
                        {
                          id: null,
                          title: t("Describe them by hand", "Von Hand beschreiben"),
                          description: t(
                            "Add each endpoint yourself: method, path and parameters.",
                            "Trage jeden Endpunkt selbst ein: Methode, Pfad und Parameter.",
                          ),
                          Icon: Pencil,
                        },
                      ].map(({ id, title, description, Icon }) => (
                        <button
                          key={id ?? "by-hand"}
                          type="button"
                          aria-pressed={state.sourceId === id}
                          onClick={() =>
                            update({
                              sourceId: id,
                              draft: id === state.sourceId ? state.draft : null,
                            })
                          }
                          className={cn(
                            "flex w-full items-start gap-3 rounded-xl border-2 p-3 text-left transition",
                            state.sourceId === id
                              ? "border-primary bg-primary/5"
                              : "border-border bg-background/30 hover:border-primary/40",
                          )}
                        >
                          <Icon className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                          <span>
                            <span className="block text-sm font-medium">{title}</span>
                            <span className="block text-[11px] text-muted-foreground">
                              {description}
                            </span>
                          </span>
                        </button>
                      ))}
                    </div>
                  </div>
                )}
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
                <AuthHeaderFields state={state} onChange={update} />
              </div>
            )}

            {sourceStepActive && activeSource && (
              <div className="space-y-4">
                <activeSource.component
                  mode="create"
                  initialDraft={state.draft}
                  existing={null}
                  onComplete={acceptDraft}
                  onCancel={() => {
                    setStepError(null);
                    setStep(0);
                  }}
                />
                {stepError && (
                  <p role="alert" className="text-xs text-[color:var(--status-error)]">
                    {stepError}
                  </p>
                )}
              </div>
            )}

            {byHandStepActive && (
              <div className="space-y-4">
                <Field label="Base URL">
                  <input
                    value={state.baseUrl}
                    onChange={(e) => update({ baseUrl: e.target.value })}
                    placeholder="https://api.example.com"
                    className="w-full rounded-md border border-border bg-background/40 px-3 py-2 font-mono text-sm outline-none focus:border-primary/50"
                  />
                </Field>
                <HttpLoginFields value={state.login} onChange={(login) => update({ login })} />
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
                        {toolProblems[i] && (
                          <p role="alert" className="text-xs text-[color:var(--status-error)]">
                            {toolProblemText(toolProblems[i])}
                          </p>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
                {stepError && (
                  <p className="text-xs text-[color:var(--status-error)]">{stepError}</p>
                )}
              </div>
            )}

            {activeStep.key === "test" && state.serverType !== "manual_http" && (
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
                {state.testedConnectionId && (
                  <McpTestLogDrawer connectionId={state.testedConnectionId} busy={busy} />
                )}
              </div>
            )}

            {activeStep.key === "test" && state.serverType === "manual_http" && (
              <HttpTestCallPanel tools={toolsForTest} run={runManualTestCall} />
            )}

            {activeStep.key === "summary" && (
              <SummaryCard state={described} departmentName={departmentName} />
            )}
          </div>

          <footer className="flex items-center justify-between gap-3 border-t border-border bg-background/30 px-6 py-4">
            <button
              onClick={handleBack}
              disabled={busy}
              className="inline-flex items-center gap-1 rounded-md px-3 py-1.5 text-sm text-muted-foreground transition hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
            >
              {step === 0 ? (
                "Cancel"
              ) : (
                <>
                  <ChevronLeft className="h-4 w-4" /> Back
                </>
              )}
            </button>
            {sourceStepActive ? null : !isLastStep ? (
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
    </>
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

function SummaryCard({
  state,
  departmentName,
}: {
  state: WizardState;
  departmentName: string | null;
}) {
  const t = useT();
  const typeLabel = state.serverType ? SERVER_TYPE_LABELS[state.serverType] : "—";
  const toolOrEndpointCount =
    state.serverType === "manual_http"
      ? (draftOf(state)?.httpTools.length ?? state.httpTools.length)
      : (state.testedToolNames?.length ?? null);
  const secretCount =
    state.serverType === "stdio"
      ? state.envRows.filter((r) => r.secret && r.key).length
      : state.serverType === "manual_http"
        ? loginSecret(loginOf(state))
          ? 1
          : 0
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
        {departmentName && (
          <Row icon={<Users className="h-3 w-3" />} label={t("Department", "Abteilung")}>
            <span className="font-medium text-foreground">{departmentName}</span>
          </Row>
        )}
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
