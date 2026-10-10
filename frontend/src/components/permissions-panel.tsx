import { ArrowDown, Lock, Plus, Search, ShieldCheck, Sparkles, Trash2, X } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import { AgentAvatar } from "@/components/agent-avatar";
import { Panel } from "@/components/app-shell";
import { CredentialPicker } from "@/components/credential-picker";
import { useConfirm } from "@/hooks/use-confirm";
import type { Agent, Department } from "@/lib/mock-data";
import {
  effectiveAgentPolicy,
  isRestricted,
  mcpToolsFromConnections,
  PERM_LABEL,
  type PermKey,
  type PolicyMap,
  type ToolPolicy,
  type McpTool,
} from "@/lib/permissions";
import { useCreateMcpLogin, useMcpConnections, useMcpLogins, type McpLoginDTO } from "@/lib/hooks";
import { cn } from "@/lib/utils";

type Mode = "department" | "agent";

interface CommonProps {
  dept: Department;
  deptPolicy: PolicyMap;
  members: Agent[];
  agentOverrides: Record<string, PolicyMap>;
}

interface DeptProps extends CommonProps {
  mode: "department";
  onDepartmentPolicyChange?: (policy: PolicyMap) => void;
}

interface AgentProps extends CommonProps {
  mode: "agent";
  agent: Agent;
}

export function PermissionsPanel(props: DeptProps | AgentProps) {
  const { dept, deptPolicy: initialDept, members, agentOverrides, mode } = props;
  const agent = mode === "agent" ? props.agent : undefined;

  // The interfaces a department can grant ARE the tenant's real MCP
  // connections — never a static vendor catalog.
  const { data: connections = [] } = useMcpConnections();
  const tools = useMemo(() => mcpToolsFromConnections(connections), [connections]);
  // The default-login picker (department mode) is the same inline "select
  // existing credential or create new" control the Hire dialog uses -- a
  // login is a Credential paired 1:1 with a tenant-global McpConnection
  // (POST /mcp/logins), grouped here by tool key exactly like there.
  const logins = useMcpLogins();
  const loginsByKey: Record<string, McpLoginDTO[]> = {};
  for (const login of logins.data ?? []) {
    (loginsByKey[login.name] ??= []).push(login);
  }
  const createLogin = useCreateMcpLogin();

  // In this prototype, the department frame is fixed in agent mode
  // and editable in department mode.
  const [dPol, setDPol] = useState<PolicyMap>(initialDept);
  const [aPol, setAPol] = useState<PolicyMap>(agent ? (agentOverrides[agent.id] ?? {}) : {});

  const effective = useMemo<PolicyMap>(
    () => (mode === "agent" ? effectiveAgentPolicy(tools, dPol, aPol) : dPol),
    [tools, dPol, aPol, mode],
  );

  function setDeptTool(toolId: string, next: ToolPolicy) {
    setDPol((prev) => {
      const policy = { ...prev, [toolId]: next };
      if (mode === "department") props.onDepartmentPolicyChange?.(policy);
      return policy;
    });
  }

  // Department-mode-only "Add tool" / "Remove tool" flow, mirroring
  // AgentToolAccessPanel (agents.$id.tsx): a tile exists only for a tool
  // this department's frame actually names -- Object.keys(dPol) IS exactly
  // that set, since fromDepartmentFrame (departments.$id.tsx) produces one
  // PolicyMap entry per frame.tools key. Showing every tenant connection
  // here (the old behaviour) made "which tools does this department
  // actually use" unreadable once a tenant had more than a handful.
  const { confirm, ConfirmDialog } = useConfirm();
  const [pickerOpen, setPickerOpen] = useState(false);
  const [toolSearch, setToolSearch] = useState("");
  const addedTools = mode === "department" ? tools.filter((t) => t.id in dPol) : tools;
  const addableTools = tools.filter((t) => !(t.id in dPol));

  function addDeptTool(toolId: string) {
    setDeptTool(toolId, {
      enabled: true,
      perms: { read: true, write: false, send: false },
      approvalEUR: null,
      defaultConnectionId: null,
    });
    setPickerOpen(false);
  }

  async function removeDeptTool(toolId: string, toolName: string) {
    const ok = await confirm({
      title: "Remove this tool?",
      description: `Remove "${toolName}" from ${dept.name}? Every agent in this department loses it immediately. It can be added again later.`,
      confirmLabel: "Remove",
      cancelLabel: "Cancel",
    });
    if (!ok) return;
    setDPol((prev) => {
      const { [toolId]: _removed, ...policy } = prev;
      if (mode === "department") props.onDepartmentPolicyChange?.(policy);
      return policy;
    });
    toast.success(`${toolName} removed from ${dept.name}`);
  }

  // Resolves a picked/created credential to a login (McpConnection) and
  // sets it as this tool's department-wide default. Reuses an existing
  // login already backed by this exact credential; otherwise creates one --
  // POST /mcp/logins 409s if a DIFFERENT credential already backs a login
  // under this tool key, surfaced as the toast below (switching credentials
  // isn't supported yet).
  async function pinDefaultCredential(
    toolId: string,
    credentialType: string,
    credentialId: string,
  ) {
    const dTool = dPol[toolId];
    if (!dTool) return;
    if (!credentialId) {
      setDeptTool(toolId, { ...dTool, defaultConnectionId: null });
      return;
    }
    const existing = (loginsByKey[toolId] ?? []).find((l) => l.credentialId === credentialId);
    if (existing) {
      setDeptTool(toolId, { ...dTool, defaultConnectionId: existing.id });
      return;
    }
    try {
      const login = await createLogin.mutateAsync({
        name: toolId,
        credentialType,
        credentialId,
        scopes: [],
      });
      setDeptTool(toolId, { ...dTool, defaultConnectionId: login.id });
    } catch (err) {
      toast.error("Could not link this credential", {
        description: err instanceof Error ? err.message : String(err),
      });
    }
  }

  function setAgentTool(toolId: string, patch: Partial<ToolPolicy>) {
    if (!agent) return;
    const dTool = dPol[toolId];
    const current = aPol[toolId] ?? {
      enabled: dTool.enabled,
      perms: { ...dTool.perms },
      approvalEUR: dTool.approvalEUR,
    };
    const merged: ToolPolicy = {
      enabled: patch.enabled ?? current.enabled,
      perms: { ...current.perms, ...(patch.perms ?? {}) },
      approvalEUR: patch.approvalEUR !== undefined ? patch.approvalEUR : current.approvalEUR,
    };
    // Clamp against department
    merged.enabled = merged.enabled && dTool.enabled;
    (Object.keys(merged.perms) as PermKey[]).forEach((k) => {
      merged.perms[k] = merged.perms[k] && dTool.perms[k];
    });
    if (dTool.approvalEUR != null) {
      merged.approvalEUR =
        merged.approvalEUR == null
          ? dTool.approvalEUR
          : Math.min(merged.approvalEUR, dTool.approvalEUR);
    }
    setAPol((prev) => ({ ...prev, [toolId]: merged }));
    toast.success(`Permissions updated for ${agent.name}`, {
      description: `${tools.find((t) => t.id === toolId)?.name}`,
    });
  }

  const enabledTools = tools.filter((t) => effective[t.id]?.enabled);

  return (
    <div className="space-y-6">
      <InheritanceHeader
        tools={tools}
        dept={dept}
        members={members}
        agent={agent}
        deptPolicy={dPol}
        agentOverrides={agentOverrides}
        highlightAgent={agent?.id}
      />

      {mode === "agent" && agent && (
        <div className="flex items-start gap-2 rounded-md border border-primary/30 bg-primary/5 p-3 text-xs text-foreground/90">
          <Sparkles className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
          <span>
            <b>{agent.name}</b> inherits the frame of department <b>{dept.name}</b>. Changes here
            apply only to {agent.name} and can only tighten the frame, never expand it.
          </span>
        </div>
      )}

      <div className="grid gap-6 xl:grid-cols-2">
        {/* 1) MCP-Kacheln */}
        <Panel className="p-5">
          {ConfirmDialog}
          <div className="flex items-start justify-between gap-2">
            <SectionHeader
              title={mode === "agent" ? "Available interfaces" : "MCP interfaces"}
              hint={
                mode === "agent"
                  ? "inherited from the department — on/off for the agent only"
                  : "only tools added here are ever offered to this department's agents"
              }
            />
            {mode === "department" && (
              <button
                type="button"
                onClick={() => {
                  setToolSearch("");
                  setPickerOpen(true);
                }}
                disabled={addableTools.length === 0}
                className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-border bg-background/40 px-2.5 py-1.5 text-xs font-medium text-foreground transition hover:bg-background/70 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <Plus className="h-3.5 w-3.5" /> Add tool
              </button>
            )}
          </div>
          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            {mode === "department" && addedTools.length === 0 && (
              <p className="col-span-full rounded-md border border-dashed border-border/70 bg-background/30 p-3 text-center text-xs text-muted-foreground">
                No tools yet — add one to grant it to every agent in this department.
              </p>
            )}
            {mode === "agent" && tools.length === 0 && (
              <p className="col-span-full text-xs text-muted-foreground">
                No MCP connections yet. Add one under Capas to grant it here.
              </p>
            )}
            {addedTools.map((tool) => {
              const dTool = dPol[tool.id];
              const eff = effective[tool.id];
              const deptAllows = dTool?.enabled;
              const isOn = eff?.enabled;
              const restrictedByAgent =
                mode === "agent" && dTool && eff && isRestricted(dTool, eff);
              const credentialType = connections.find((c) => c.name === tool.id)?.credentialType;
              const pickedCredentialId =
                (loginsByKey[tool.id] ?? []).find((l) => l.id === dTool?.defaultConnectionId)
                  ?.credentialId ?? "";
              return (
                <ToolTile
                  key={tool.id}
                  tool={tool}
                  on={!!isOn}
                  disabled={mode === "agent" && !deptAllows}
                  restricted={!!restrictedByAgent}
                  onRemove={
                    mode === "department" ? () => removeDeptTool(tool.id, tool.name) : undefined
                  }
                  onToggle={(next) => {
                    if (mode === "department") {
                      setDeptTool(tool.id, {
                        ...(dTool ?? {
                          enabled: false,
                          perms: { read: false, write: false, send: false },
                          approvalEUR: null,
                          defaultConnectionId: null,
                        }),
                        enabled: next,
                      });
                      toast.success(
                        `${tool.name} ${next ? "enabled" : "disabled"} for ${dept.name}`,
                      );
                    } else {
                      setAgentTool(tool.id, { enabled: next });
                    }
                  }}
                >
                  {mode === "department" && dTool?.enabled && credentialType && (
                    <div
                      className="w-full min-w-0 border-t border-border/60 pt-2"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <div className="mb-1 text-[10px] uppercase tracking-widest text-muted-foreground">
                        Default login
                      </div>
                      <CredentialPicker
                        credentialType={credentialType}
                        value={pickedCredentialId}
                        onChange={(credentialId) =>
                          pinDefaultCredential(tool.id, credentialType, credentialId)
                        }
                      />
                    </div>
                  )}
                </ToolTile>
              );
            })}
          </div>
        </Panel>

        {/* 2) Permissions per tool */}
        <Panel className="p-5">
          <SectionHeader
            title="Permissions"
            hint={
              mode === "agent"
                ? "restrict-only — locked = not allowed at department level"
                : "Upper bound for all agents in this department"
            }
          />
          <div className="mt-4 space-y-3">
            {enabledTools.length === 0 && (
              <p className="rounded-md border border-dashed border-border/70 bg-background/30 p-4 text-center text-xs text-muted-foreground">
                No active interfaces. Enable a tool first.
              </p>
            )}
            {enabledTools.map((tool) => {
              const dTool = dPol[tool.id];
              const eff = effective[tool.id];
              const Icon = tool.icon;
              const restrictedByAgent =
                mode === "agent" && dTool && eff && isRestricted(dTool, eff);
              return (
                <div
                  key={tool.id}
                  className={cn(
                    "rounded-lg border p-3",
                    restrictedByAgent
                      ? "border-primary/40 bg-primary/[0.04]"
                      : "border-border bg-background/30",
                  )}
                >
                  <div className="mb-2 flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2 text-sm font-medium">
                      <Icon className="h-4 w-4 text-muted-foreground" />
                      {tool.name}
                    </div>
                    <InheritanceBadge restricted={!!restrictedByAgent} mode={mode} />
                  </div>
                  <div className="flex flex-wrap items-center gap-1.5">
                    {(Object.keys(PERM_LABEL) as PermKey[]).map((k) => {
                      const deptAllows = dTool.perms[k];
                      const on = !!eff.perms[k];
                      const lockedByDept = mode === "agent" && !deptAllows;
                      return (
                        <PermChip
                          key={k}
                          label={PERM_LABEL[k]}
                          on={on}
                          locked={lockedByDept}
                          onClick={() => {
                            if (mode === "department") {
                              setDeptTool(tool.id, {
                                ...dTool,
                                perms: { ...dTool.perms, [k]: !on },
                              });
                              return;
                            }
                            if (lockedByDept) return;
                            setAgentTool(tool.id, {
                              perms: { [k]: !on } as Record<PermKey, boolean>,
                            });
                          }}
                        />
                      );
                    })}
                  </div>
                  <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
                    <span className="text-muted-foreground">Approval needed from</span>
                    <div className="inline-flex items-center overflow-hidden rounded-md border border-border bg-background/50">
                      <span className="border-r border-border px-2 py-1 font-mono text-foreground/70">
                        €
                      </span>
                      <input
                        type="number"
                        min={0}
                        step={100}
                        placeholder="—"
                        value={eff.approvalEUR ?? ""}
                        onChange={(e) => {
                          const raw = e.target.value;
                          const next = raw === "" ? null : Math.max(0, Number(raw));
                          if (mode === "department") {
                            setDeptTool(tool.id, { ...dTool, approvalEUR: next });
                          } else {
                            // Clamp against department, wenn diese eine Schwelle hat
                            const clamped =
                              dTool.approvalEUR != null && next != null
                                ? Math.min(next, dTool.approvalEUR)
                                : next;
                            setAgentTool(tool.id, { approvalEUR: clamped });
                          }
                        }}
                        className="w-24 bg-transparent px-2 py-1 font-mono text-sm outline-none"
                      />
                    </div>
                    {mode === "agent" && dTool.approvalEUR != null && (
                      <span className="rounded-full border border-border bg-background/40 px-2 py-0.5 text-[10px] text-muted-foreground">
                        Department: max €{dTool.approvalEUR.toLocaleString("en-US")}
                      </span>
                    )}
                    {mode === "agent" && dTool.approvalEUR == null && (
                      <span className="rounded-full border border-border bg-background/40 px-2 py-0.5 text-[10px] text-muted-foreground">
                        Department: no threshold
                      </span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
          <div className="mt-4 flex items-start gap-2 rounded-md border border-[color:var(--status-warning)]/30 bg-[color:var(--status-warning)]/5 p-3 text-xs text-foreground/90">
            <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-[color:var(--status-warning)]" />
            <span>
              {mode === "department" ? (
                <>
                  This frame is the <b>upper bound</b>. Individual agents can be further restricted,
                  but <b>not expanded</b>.
                </>
              ) : (
                <>
                  Permissions are always the <b>intersection</b> with the department frame.
                  Inherited limits are locked.
                </>
              )}
            </span>
          </div>
        </Panel>
      </div>

      {mode === "department" &&
        pickerOpen &&
        (() => {
          const searchTerm = toolSearch.trim().toLowerCase();
          const filtered = searchTerm
            ? addableTools.filter((t) => t.name.toLowerCase().includes(searchTerm))
            : addableTools;
          return (
            <div
              className="fixed inset-0 z-40 grid place-items-center bg-black/60 p-4 backdrop-blur-sm"
              onClick={() => setPickerOpen(false)}
            >
              <div
                className="w-full max-w-lg overflow-hidden rounded-xl border border-border bg-panel shadow-2xl"
                onClick={(e) => e.stopPropagation()}
              >
                <div className="flex items-center justify-between border-b border-border px-5 py-4">
                  <div>
                    <div className="text-[11px] uppercase tracking-widest text-muted-foreground">
                      Tenant connections
                    </div>
                    <h2 className="font-serif text-xl">Add tool</h2>
                  </div>
                  <button
                    type="button"
                    onClick={() => setPickerOpen(false)}
                    className="grid h-8 w-8 place-items-center rounded-md border border-border text-muted-foreground transition hover:text-foreground"
                  >
                    <X className="h-4 w-4" />
                  </button>
                </div>
                <div className="relative border-b border-border px-5 py-3">
                  <Search className="pointer-events-none absolute left-8 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
                  <input
                    value={toolSearch}
                    onChange={(e) => setToolSearch(e.target.value)}
                    placeholder="Search tools…"
                    className="w-full rounded-md border border-border bg-background/40 py-2 pl-8 pr-3 text-sm outline-none focus:border-primary/50"
                  />
                </div>
                <div className="max-h-[60vh] divide-y divide-border overflow-y-auto">
                  {filtered.length === 0 && (
                    <div className="p-6 text-center text-sm text-muted-foreground">
                      {addableTools.length === 0
                        ? "Every tenant connection already has a tile here."
                        : "No tools match your search."}
                    </div>
                  )}
                  {filtered.map((tool) => {
                    const Icon = tool.icon;
                    return (
                      <button
                        key={tool.id}
                        type="button"
                        onClick={() => addDeptTool(tool.id)}
                        className="flex w-full items-center gap-3 px-5 py-3 text-left transition hover:bg-primary/5"
                      >
                        <Icon className="h-4 w-4 shrink-0 text-primary" />
                        <span className="min-w-0 flex-1 truncate text-sm font-medium">
                          {tool.name}
                        </span>
                        <Plus className="h-4 w-4 text-muted-foreground" />
                      </button>
                    );
                  })}
                </div>
              </div>
            </div>
          );
        })()}
    </div>
  );
}

function SectionHeader({ title, hint }: { title: string; hint: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground">{hint}</div>
      <h3 className="mt-0.5 font-serif text-lg">{title}</h3>
    </div>
  );
}

function ToolTile({
  tool,
  on,
  disabled,
  restricted,
  onToggle,
  onRemove,
  children,
}: {
  tool: McpTool;
  on: boolean;
  disabled?: boolean;
  restricted?: boolean;
  onToggle: (next: boolean) => void;
  onRemove?: () => void;
  children?: React.ReactNode;
}) {
  const Icon = tool.icon;
  return (
    <div
      className={cn(
        "group relative flex flex-col items-start gap-2 rounded-lg border p-3 text-left transition",
        disabled
          ? "cursor-not-allowed border-dashed border-border/60 bg-background/20 text-muted-foreground/70"
          : on
            ? restricted
              ? "border-primary/50 bg-primary/[0.06] hover:border-primary"
              : "border-primary/50 bg-primary/[0.06] hover:border-primary"
            : "border-border bg-background/40 hover:border-primary/40",
      )}
    >
      <button
        type="button"
        onClick={() => !disabled && onToggle(!on)}
        disabled={disabled}
        title={disabled ? "Not enabled at department level" : undefined}
        className="flex w-full flex-col items-start gap-2 text-left disabled:cursor-not-allowed"
      >
        <div className="flex w-full items-center justify-between">
          <div className="flex items-center gap-2">
            <div
              className={cn(
                "grid h-8 w-8 place-items-center rounded-md",
                on && !disabled
                  ? "bg-primary/15 text-primary"
                  : "bg-background/60 text-muted-foreground",
              )}
            >
              <Icon className="h-4 w-4" />
            </div>
            <div className="font-medium text-foreground">{tool.name}</div>
          </div>
          {disabled ? (
            <Lock className="h-3.5 w-3.5 text-muted-foreground" />
          ) : (
            <MiniToggle on={on} />
          )}
        </div>
        <div className="text-xs text-muted-foreground">{tool.desc}</div>
      </button>
      <div className="flex w-full items-center gap-1.5">
        <span className="rounded-full border border-primary/30 bg-primary/10 px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-widest text-primary">
          via MCP
        </span>
        {disabled && (
          <span className="rounded-full border border-border bg-background/40 px-1.5 py-0.5 text-[9px] text-muted-foreground">
            not enabled at department level
          </span>
        )}
        {onRemove && (
          <button
            type="button"
            onClick={() => onRemove()}
            title="Remove tool"
            className="ml-auto inline-flex items-center rounded-md p-1 text-muted-foreground transition hover:text-destructive"
          >
            <Trash2 className="h-3 w-3" />
          </button>
        )}
      </div>
      {children}
    </div>
  );
}

function MiniToggle({ on }: { on: boolean }) {
  return (
    <span
      className={cn(
        "inline-flex h-4 w-7 items-center rounded-full border transition",
        on ? "border-primary bg-primary/60" : "border-border bg-background/60",
      )}
    >
      <span
        className={cn(
          "h-3 w-3 rounded-full bg-foreground transition-transform",
          on ? "translate-x-3" : "translate-x-0.5",
        )}
      />
    </span>
  );
}

function PermChip({
  label,
  on,
  locked,
  onClick,
}: {
  label: string;
  on: boolean;
  locked?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={locked}
      title={locked ? "Not allowed by the department" : undefined}
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-1 text-[11px] transition",
        locked
          ? "cursor-not-allowed border-dashed border-border/60 bg-background/30 text-muted-foreground/60"
          : on
            ? "border-primary/60 bg-primary/15 text-primary"
            : "border-border bg-background/50 text-muted-foreground hover:text-foreground",
      )}
    >
      {locked && <Lock className="h-3 w-3" />}
      {label}
    </button>
  );
}

function InheritanceBadge({ restricted, mode }: { restricted: boolean; mode: Mode }) {
  if (mode === "department") {
    return (
      <span className="rounded-full border border-border bg-background/40 px-2 py-0.5 text-[10px] uppercase tracking-widest text-muted-foreground">
        Frame
      </span>
    );
  }
  return restricted ? (
    <span className="rounded-full border border-primary/40 bg-primary/10 px-2 py-0.5 text-[10px] uppercase tracking-widest text-primary">
      tightened by agent
    </span>
  ) : (
    <span className="rounded-full border border-border bg-background/40 px-2 py-0.5 text-[10px] uppercase tracking-widest text-muted-foreground">
      inherited
    </span>
  );
}

function InheritanceHeader({
  tools,
  dept,
  members,
  agent,
  deptPolicy,
  agentOverrides,
  highlightAgent,
}: {
  tools: McpTool[];
  dept: Department;
  members: Agent[];
  agent?: Agent;
  deptPolicy: PolicyMap;
  agentOverrides: Record<string, PolicyMap>;
  highlightAgent?: string;
}) {
  const activeCount = tools.filter((t) => deptPolicy[t.id]?.enabled).length;
  return (
    <Panel className="relative overflow-hidden p-5">
      <div
        className="pointer-events-none absolute inset-0"
        style={{
          background: `radial-gradient(ellipse at top left, color-mix(in oklab, ${dept.accent} 12%, transparent), transparent 65%)`,
        }}
      />
      <div className="relative">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
              Inheritance
            </div>
            <h3 className="font-serif text-lg">
              Department <span style={{ color: dept.accent }}>{dept.name}</span> = Frame
            </h3>
          </div>
          <span className="rounded-full border border-border bg-background/40 px-2 py-1 text-[11px] text-muted-foreground">
            {activeCount} of {tools.length} MCP interfaces active
          </span>
        </div>

        <div
          className="mt-4 rounded-xl border-2 border-dashed p-4"
          style={{
            borderColor: `color-mix(in oklab, ${dept.accent} 45%, transparent)`,
            background: `color-mix(in oklab, ${dept.accent} 6%, transparent)`,
          }}
        >
          <div className="flex flex-wrap items-center gap-2">
            {members.map((m) => {
              const override = agentOverrides[m.id];
              const eff = effectiveAgentPolicy(tools, deptPolicy, override);
              const restricted = tools.some((t) => {
                const d = deptPolicy[t.id];
                const e = eff[t.id];
                return d && e && isRestricted(d, e);
              });
              const active = highlightAgent === m.id;
              return (
                <div
                  key={m.id}
                  className={cn(
                    "flex items-center gap-2 rounded-md border bg-background/60 px-2 py-1.5 text-xs",
                    active
                      ? "border-primary text-foreground shadow-[0_0_0_2px_color-mix(in_oklab,var(--primary)_25%,transparent)]"
                      : "border-border text-muted-foreground",
                  )}
                >
                  <AgentAvatar seed={m.id} size={20} title={m.name} />
                  <span className="font-medium text-foreground">{m.name}</span>
                  {restricted ? (
                    <span className="rounded-full border border-primary/40 bg-primary/10 px-1.5 py-0.5 text-[9px] uppercase tracking-widest text-primary">
                      tighter
                    </span>
                  ) : (
                    <span className="rounded-full border border-border bg-background/40 px-1.5 py-0.5 text-[9px] uppercase tracking-widest text-muted-foreground">
                      full frame
                    </span>
                  )}
                </div>
              );
            })}
          </div>
          <div className="mt-3 flex items-center gap-2 text-[11px] text-muted-foreground">
            <ArrowDown className="h-3.5 w-3.5" />
            <span>
              inherits ↓ agents can only <b>tighten</b>, never expand — the frame stays the upper
              bound.
            </span>
          </div>
        </div>

        {agent && (
          <div className="mt-3 text-[11px] text-muted-foreground">
            Focus: <span className="text-foreground">{agent.name}</span> · {agent.role}
          </div>
        )}
      </div>
    </Panel>
  );
}
