import { BookOpen, Info, Lock, ShieldAlert } from "lucide-react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { useCreateGrant } from "@/lib/hooks";
import { sensitivityMeta, type KnowledgeBase } from "@/lib/mock-data";
import { cn } from "@/lib/utils";

type Mode = "department" | "agent";

interface Props {
  mode: Mode;
  /** knowledge bases to render (real data from useKnowledgeBases) */
  bases: KnowledgeBase[];
  /** dept id */
  departmentId?: string;
  /** agent id (only for mode="agent") */
  agentId?: string;
  agentName?: string;
  /** which KB ids are currently enabled at this scope (derive from the real
   * KB DTO's linkedDepartments/linkedAgents — the source of truth) */
  enabled: string[];
  /**
   * Called after a grant is successfully created (enable only — there is no
   * delete-grant endpoint, so this is never invoked to turn an assignment
   * off). Optional: callers whose `enabled` list is itself derived from
   * useKnowledgeBases() don't need it, since the query invalidation this
   * component triggers on success already refreshes that data.
   * Omit (or ignore) when readOnly is true — no toggle is rendered.
   */
  onToggle?: (kbId: string, next: boolean) => void;
  /** for agent mode: which KBs the department allows (agent can only tighten) */
  departmentEnabled?: string[];
  /**
   * Render as a display-only list: no interactive toggle, no toast on click.
   * There is no delete-grant endpoint yet, so a bidirectional toggle can't be
   * honestly persisted for agent-level KB assignment — show real state instead.
   */
  readOnly?: boolean;
}

export function KnowledgeAssignment({
  mode,
  bases,
  departmentId,
  agentId,
  agentName,
  enabled,
  onToggle,
  departmentEnabled,
  readOnly = false,
}: Props) {
  const createGrant = useCreateGrant();
  const granteeId = mode === "department" ? departmentId : agentId;

  return (
    <Panel className="p-5">
      <header className="mb-4 flex items-start justify-between gap-4">
        <div className="flex items-start gap-3">
          <div className="grid h-9 w-9 place-items-center rounded-md bg-primary/15 text-primary">
            <BookOpen className="h-4 w-4" />
          </div>
          <div>
            <div className="font-serif text-lg leading-tight">Knowledge</div>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {readOnly
                ? `Knowledge bases linked to ${agentName ?? "this agent"}. Read-only — managed via knowledge grants.`
                : mode === "department"
                  ? "Assign knowledge bases to this department. Agents inherit these and can only tighten access, never expand it."
                  : `Assign knowledge bases directly to ${agentName ?? "this agent"}. Bases inherited from the department are marked and always active.`}
            </p>
          </div>
        </div>
        <div className="hidden items-center gap-1.5 rounded-md border border-border bg-background/40 px-2 py-1 text-xs text-muted-foreground sm:inline-flex">
          <Info className="h-3 w-3" />
          {readOnly ? "Read-only" : "Department = inherited · Agent = additional"}
        </div>
      </header>

      <ul className="space-y-2">
        {bases.map((kb) => {
          const sens = sensitivityMeta[kb.sensitivity];
          const inheritedFromDept = mode === "agent" && !!departmentEnabled?.includes(kb.id);
          const on = enabled.includes(kb.id) || inheritedFromDept;
          return (
            <li
              key={kb.id}
              className={cn(
                "relative flex items-center gap-3 rounded-md border px-3 py-2.5 pl-4 transition",
                inheritedFromDept
                  ? "border-primary/40 bg-primary/[0.06]"
                  : "border-border bg-background/30",
              )}
            >
              {inheritedFromDept && (
                <span
                  aria-hidden
                  className="absolute inset-y-1 left-0 w-1 rounded-r-full bg-primary/70"
                />
              )}
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate text-sm font-medium">{kb.name}</span>
                  <SensitivityBadge kb={kb} />
                  {inheritedFromDept && (
                    <span className="inline-flex items-center gap-1 rounded-full border border-primary/50 bg-primary/15 px-1.5 py-0.5 micro-label text-primary">
                      inherited from dept
                    </span>
                  )}
                  {kb.localOnly && (
                    <span
                      className="inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-micro"
                      style={{
                        color: sens.color,
                        borderColor: `color-mix(in oklab, ${sens.color} 40%, transparent)`,
                        background: `color-mix(in oklab, ${sens.color} 10%, transparent)`,
                      }}
                    >
                      <Lock className="h-2.5 w-2.5" /> local model
                    </span>
                  )}
                </div>
                <p className="mt-0.5 truncate text-xs text-muted-foreground">
                  {kb.docs.toLocaleString()} docs · roles: {kb.roles.join(", ")}
                </p>
              </div>
              <ToggleSwitch
                on={!!on}
                // Once granted, an assignment can't be revoked from the UI —
                // there is no delete-grant endpoint. Show it as a fixed,
                // checked, disabled control rather than a toggle that appears
                // to turn off but doesn't. The "inherited from department"
                // indicator stays clickable (it just points the user at
                // where to manage it), so it's excluded from this lock.
                disabled={
                  readOnly ||
                  (on && !inheritedFromDept) ||
                  (createGrant.isPending && createGrant.variables?.kbId === kb.id)
                }
                title={
                  readOnly
                    ? on
                      ? "Enabled — read-only"
                      : "Disabled — read-only"
                    : inheritedFromDept
                      ? "Inherited from the department — manage it there"
                      : on
                        ? "Removing a grant is not yet supported"
                        : "Enable knowledge base"
                }
                onClick={() => {
                  if (readOnly || on) return;
                  if (inheritedFromDept) {
                    toast("Inherited from the department", {
                      description: `${kb.name} is enabled at the department level. Manage it there to remove.`,
                    });
                    return;
                  }
                  if (!granteeId) return;
                  createGrant.mutate(
                    { kbId: kb.id, granteeType: mode, granteeId },
                    {
                      onSuccess: () => {
                        onToggle?.(kb.id, true);
                        toast.success(
                          mode === "department"
                            ? `${kb.name} linked to department`
                            : `${kb.name} enabled for ${agentName ?? "agent"}`,
                        );
                      },
                      onError: () => toast.error("Couldn't grant access", { description: kb.name }),
                    },
                  );
                }}
              />
            </li>
          );
        })}
      </ul>
    </Panel>
  );
}

function SensitivityBadge({ kb }: { kb: KnowledgeBase }) {
  const sens = sensitivityMeta[kb.sensitivity];
  const isRestricted = kb.sensitivity === "restricted";
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-micro"
      style={{
        color: sens.color,
        borderColor: `color-mix(in oklab, ${sens.color} 40%, transparent)`,
        background: `color-mix(in oklab, ${sens.color} 10%, transparent)`,
      }}
    >
      {isRestricted && <ShieldAlert className="h-2.5 w-2.5" />}
      {sens.label}
    </span>
  );
}

function ToggleSwitch({
  on,
  disabled,
  title,
  onClick,
}: {
  on: boolean;
  disabled?: boolean;
  title?: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-pressed={on}
      className={cn(
        "relative h-5 w-9 shrink-0 rounded-full border transition",
        on ? "border-primary bg-primary/40" : "border-border bg-background/40",
        disabled && "cursor-not-allowed opacity-80",
      )}
    >
      <span
        className={cn(
          "absolute top-0.5 h-3.5 w-3.5 rounded-full transition-all",
          on
            ? "left-4 bg-primary shadow-[0_0_10px_var(--primary)]"
            : "left-0.5 bg-muted-foreground",
        )}
      />
    </button>
  );
}
