// Real MCP connections (POST /mcp/connections, POST /mcp/connections/{id}/test).
// Distinct from the mock integration catalog on the same page: this panel only
// ever shows what the backend actually reported — an untested connection reads
// "untested", never a fake "connected".
import { ChevronDown, ChevronRight, Plus, RefreshCw, Server, Trash2, X } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { McpTestLogDrawer } from "@/components/mcp-test-log-drawer";
import { useConfirm } from "@/hooks/use-confirm";
import {
  useCreateMcpConnection,
  useDeleteMcpConnectionById,
  useDepartments,
  useMcpConnections,
  useTestMcpConnection,
  type McpConnection,
} from "@/lib/hooks";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

type HealthStatus = "untested" | "ok" | "error";

export function healthStatusOf(c: McpConnection): HealthStatus {
  const status = c.health?.status;
  if (status === "ok" && c.connected) return "ok";
  if (status === "error") return "error";
  return "untested";
}

export function McpConnectionsPanel() {
  const t = useT();
  const { data: connections = [], isLoading } = useMcpConnections();
  const [createOpen, setCreateOpen] = useState(false);

  return (
    <section>
      <div className="mb-3 flex items-center gap-2">
        <Server className="h-4 w-4 text-muted-foreground" />
        <h2 className="font-serif text-lg">
          {t("MCP Connections", "MCP-Verbindungen")}{" "}
          <span className="text-muted-foreground">({connections.length})</span>
        </h2>
        <span className="rounded-full border border-border bg-background/30 px-2 py-0.5 text-[10px] uppercase tracking-wider text-muted-foreground">
          {t("Live", "Live")}
        </span>
        <button
          onClick={() => setCreateOpen(true)}
          className="ml-auto inline-flex items-center gap-1 rounded-md bg-primary px-2.5 py-1.5 text-xs font-medium text-primary-foreground"
        >
          <Plus className="h-3 w-3" /> {t("Add connection", "Verbindung anlegen")}
        </button>
      </div>
      <p className="mb-3 text-xs text-muted-foreground">
        {t(
          "Real server connections agents can use as tools — separate from the catalog above.",
          "Echte Serververbindungen, die Agenten als Tools nutzen können – getrennt vom Katalog oben.",
        )}
      </p>
      {isLoading ? (
        <Panel className="p-5 text-sm text-muted-foreground">{t("Loading…", "Lädt…")}</Panel>
      ) : connections.length === 0 ? (
        <Panel className="p-5 text-sm text-muted-foreground">
          {t("No MCP connections yet.", "Noch keine MCP-Verbindungen.")}
        </Panel>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {connections.map((c) => (
            <McpConnectionCard key={c.id} connection={c} />
          ))}
        </div>
      )}
      {createOpen && <CreateConnectionDialog onClose={() => setCreateOpen(false)} />}
    </section>
  );
}

function CreateConnectionDialog({ onClose }: { onClose: () => void }) {
  // Department picker for the new connection, not a paginated list view.
  const { data: departmentsPage } = useDepartments({ pageSize: 200 });
  const departments = departmentsPage?.items ?? [];
  const create = useCreateMcpConnection();
  const [name, setName] = useState("MCP server");
  const [transport, setTransport] = useState<"stdio" | "http" | "sse">("stdio");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [serverUrl, setServerUrl] = useState("");
  const [departmentId, setDepartmentId] = useState("");
  async function submit() {
    if (!name.trim() || (transport === "stdio" ? !command.trim() : !serverUrl.trim())) return;
    try {
      await create.mutateAsync({
        name: name.trim(),
        transport,
        command: command.trim(),
        args: args ? args.split(" ").filter(Boolean) : [],
        serverUrl: serverUrl.trim(),
        departmentId: departmentId || null,
        scopes: [],
      });
      toast.success("MCP connection created", { description: name });
      onClose();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not create connection");
    }
  }
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4" onClick={onClose}>
      <div
        className="w-full max-w-lg rounded-xl border border-border bg-panel p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-start justify-between">
          <div>
            <h3 className="font-serif text-lg">Add MCP connection</h3>
            <p className="mt-1 text-xs text-muted-foreground">
              Use the server's exact start command or URL. Credentials stay in encrypted secrets and
              are resolved only by the backend.
            </p>
          </div>
          <button onClick={onClose}>
            <X className="h-4 w-4" />
          </button>
        </header>
        <div className="mt-4 space-y-3">
          <label className="block text-xs text-muted-foreground">
            Name
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm"
            />
          </label>
          <label className="block text-xs text-muted-foreground">
            Transport
            <select
              value={transport}
              onChange={(e) => setTransport(e.target.value as typeof transport)}
              className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm"
            >
              <option value="stdio">Local command (stdio)</option>
              <option value="http">HTTP</option>
              <option value="sse">SSE</option>
            </select>
          </label>
          {transport === "stdio" ? (
            <>
              <label className="block text-xs text-muted-foreground">
                Command
                <input
                  value={command}
                  onChange={(e) => setCommand(e.target.value)}
                  placeholder="npx -y …"
                  className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 font-mono text-sm"
                />
              </label>
              <label className="block text-xs text-muted-foreground">
                Arguments (space-separated)
                <input
                  value={args}
                  onChange={(e) => setArgs(e.target.value)}
                  className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 font-mono text-sm"
                />
              </label>
            </>
          ) : (
            <label className="block text-xs text-muted-foreground">
              Server URL
              <input
                value={serverUrl}
                onChange={(e) => setServerUrl(e.target.value)}
                placeholder="https://…"
                className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 font-mono text-sm"
              />
            </label>
          )}
          <label className="block text-xs text-muted-foreground">
            Department
            <select
              value={departmentId}
              onChange={(e) => setDepartmentId(e.target.value)}
              className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm"
            >
              <option value="">Unassigned</option>
              {departments.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name}
                </option>
              ))}
            </select>
          </label>
        </div>
        <footer className="mt-5 flex justify-end gap-2">
          <button onClick={onClose} className="rounded-md border border-border px-3 py-1.5 text-xs">
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={create.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-40"
          >
            Create & test next
          </button>
        </footer>
      </div>
    </div>
  );
}

function McpConnectionCard({ connection }: { connection: McpConnection }) {
  return (
    <Panel className="flex flex-col p-5">
      <div className="flex items-center gap-3">
        <div className="grid h-10 w-10 shrink-0 place-items-center rounded-lg border border-border bg-background/40 text-muted-foreground">
          <Server className="h-4 w-4" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate font-medium">{connection.name}</div>
          <div className="truncate text-xs text-muted-foreground">
            {connection.transport}
            {connection.command ? ` · ${connection.command}` : ""}
          </div>
        </div>
      </div>
      <div className="mt-4">
        <McpConnectionBody connection={connection} />
      </div>
    </Panel>
  );
}

// The status/test/tools body, without a name header or outer Panel border --
// used standalone above (wrapped in its own Panel) and embedded directly
// inside a Capa's own card on /capas, where the Capa's name is already the
// card's header and a second bordered box nested inside it would be noise.
export function McpConnectionBody({ connection }: { connection: McpConnection }) {
  const t = useT();
  const testConnection = useTestMcpConnection(connection.id);
  const deleteConnection = useDeleteMcpConnectionById();
  // Each McpConnectionBody is already its own component instance per row (one
  // per `.map()` iteration in every caller, e.g. CapaDetailSheet's multi-
  // connection Vorschau) -- a local useConfirm() here is one dialog per row,
  // exactly what its own doc comment asks for, no lifting to a shared parent
  // needed.
  const { confirm, ConfirmDialog } = useConfirm();
  // Collapsed by default -- a connection with 40+ discovered tools otherwise
  // dominates the page's scroll height before the reader even reaches the
  // Capas below it.
  const [toolsOpen, setToolsOpen] = useState(false);
  const status = healthStatusOf(connection);
  const tools = Array.isArray(connection.health?.tools)
    ? (connection.health.tools as unknown[]).filter((x): x is string => typeof x === "string")
    : [];
  const toolCount =
    typeof connection.health?.toolCount === "number" ? connection.health.toolCount : tools.length;
  const error = typeof connection.health?.error === "string" ? connection.health.error : null;

  async function onDelete() {
    const ok = await confirm({
      title: t("Delete connection?", "Verbindung löschen?"),
      description: t(
        `Delete "${connection.name}"? Any agent whose tools still reference it will simply find nothing to connect to.`,
        `„${connection.name}" löschen? Ein Agent, dessen Tools noch darauf verweisen, findet dann schlicht keine Verbindung mehr.`,
      ),
      confirmLabel: t("Delete", "Löschen"),
      cancelLabel: t("Cancel", "Abbrechen"),
    });
    if (!ok) return;
    deleteConnection.mutate(connection.id, {
      onSuccess: () => toast.success(t("Deleted", "Gelöscht")),
      onError: (e: unknown) =>
        toast.error(
          e instanceof Error ? e.message : t("Could not delete.", "Konnte nicht gelöscht werden."),
        ),
    });
  }

  return (
    <div>
      {ConfirmDialog}
      <div className="flex items-center justify-between">
        <HealthBadge status={status} />
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={onDelete}
            disabled={deleteConnection.isPending}
            title={t("Delete connection", "Verbindung löschen")}
            className="inline-flex items-center rounded-md border border-border p-1.5 text-muted-foreground hover:border-destructive/40 hover:text-destructive disabled:opacity-50"
          >
            <Trash2 className="h-3 w-3" />
          </button>
          <button
            onClick={() => testConnection.mutate()}
            disabled={testConnection.isPending}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground disabled:opacity-50"
          >
            <RefreshCw className={cn("h-3 w-3", testConnection.isPending && "animate-spin")} />
            {testConnection.isPending
              ? t("Testing…", "Teste…")
              : t("Test connection", "Verbindung testen")}
          </button>
        </div>
      </div>

      {status === "ok" && tools.length > 0 && (
        <div className="mt-3">
          <button
            type="button"
            onClick={() => setToolsOpen((o) => !o)}
            className="mb-1 inline-flex items-center gap-1 text-[10px] uppercase tracking-widest text-muted-foreground hover:text-foreground"
          >
            {toolsOpen ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            {t("Discovered tools", "Erkannte Tools")} ({toolCount})
          </button>
          {toolsOpen && (
            <div className="flex flex-wrap gap-1">
              {tools.map((tool) => (
                <span
                  key={tool}
                  className="rounded-full border border-border bg-background/30 px-2 py-0.5 text-[10px] text-muted-foreground"
                >
                  {tool}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {status === "error" && error && (
        <p className="mt-3 text-xs text-[color:var(--status-error)]">{error}</p>
      )}

      <McpTestLogDrawer connectionId={connection.id} busy={testConnection.isPending} />
    </div>
  );
}

function HealthBadge({ status }: { status: HealthStatus }) {
  const t = useT();
  const meta: Record<HealthStatus, { color: string; label: string }> = {
    untested: { color: "var(--status-paused)", label: t("Untested", "Ungetestet") },
    ok: { color: "var(--status-running)", label: t("Connected", "Verbunden") },
    error: { color: "var(--status-error)", label: t("Error", "Fehler") },
  };
  const m = meta[status];
  return (
    <span className="inline-flex items-center gap-1.5 text-xs" style={{ color: m.color }}>
      <span
        className="h-1.5 w-1.5 rounded-full"
        style={{
          background: m.color,
          boxShadow: status === "ok" ? `0 0 8px ${m.color}` : "none",
        }}
      />
      {m.label}
    </span>
  );
}
