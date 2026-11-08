import {
  AlertTriangle,
  Bell,
  CheckCircle2,
  ChevronRight,
  Clock,
  Info,
  Sparkles,
  X,
  XCircle,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { useActivity, useAgents, useDepartments } from "@/lib/hooks";
import { agents, type ActivityItem } from "@/lib/mock-data";
import { cn } from "@/lib/utils";

// The approvals half of this file is GONE, not merely unmounted: it was a sheet
// mounted globally in app-shell with no URL of its own, and it listed and decided
// approvals with no idea whose they were. Its replacement is the route
// `src/routes/workspace.tsx`. Two inboxes over one queue is how the two drift,
// so there is only one.
//
// What is left here is NotificationsSheet.

// ============= Notifications =============

type Notif = ActivityItem & { read: boolean };

export function NotificationsSheet({
  open,
  onOpenChange,
  onOpenApprovals,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onOpenApprovals: () => void;
}) {
  const { data: activityData } = useActivity({ limit: 50 });
  const { data: agentsPage } = useAgents({ pageSize: 200 });
  const { data: departmentsPage } = useDepartments({ pageSize: 200 });
  const [items, setItems] = useState<Notif[]>([]);
  useEffect(() => {
    if (!activityData) return;
    setItems((prev) => {
      const prevById = new Map(prev.map((i) => [i.id, i]));
      return activityData.map((a, i) => prevById.get(a.id) ?? { ...a, read: i > 3 });
    });
  }, [activityData]);
  const [filter, setFilter] = useState<"all" | "warning" | "error" | "info">("all");

  const filtered = useMemo(() => {
    if (filter === "all") return items;
    return items.filter((i) => i.status === filter);
  }, [items, filter]);

  const unread = items.filter((i) => !i.read).length;

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="w-full sm:max-w-[min(92vw,520px)] border-border bg-background p-0"
      >
        <SheetHeader className="border-b border-border px-6 py-4 text-left">
          <div className="flex items-center gap-2">
            <div className="grid h-8 w-8 place-items-center rounded-md bg-primary/15 text-primary">
              <Bell className="h-4 w-4" />
            </div>
            <div className="min-w-0 flex-1">
              <SheetTitle className="font-serif text-xl leading-tight">Notifications</SheetTitle>
              <SheetDescription className="text-xs text-muted-foreground">
                {unread === 0
                  ? "You're all caught up."
                  : `${unread} unread · latest agent activity`}
              </SheetDescription>
            </div>
            <button
              type="button"
              onClick={() => setItems((prev) => prev.map((i) => ({ ...i, read: true })))}
              className="rounded-md border border-border bg-background/40 px-2 py-1 text-xs text-muted-foreground transition hover:text-foreground"
            >
              Mark all read
            </button>
          </div>

          <div className="mt-3 flex flex-wrap gap-1">
            {(["all", "warning", "error", "info"] as const).map((f) => (
              <button
                key={f}
                type="button"
                onClick={() => setFilter(f)}
                className={cn(
                  "rounded-full border px-2.5 py-1 text-xs capitalize transition",
                  filter === f
                    ? "border-primary/40 bg-primary/10 text-primary"
                    : "border-border bg-background/40 text-muted-foreground hover:text-foreground",
                )}
              >
                {f === "warning" ? "approvals" : f}
              </button>
            ))}
          </div>
        </SheetHeader>

        {filtered.length === 0 ? (
          <EmptyState
            icon={<Sparkles className="h-6 w-6 text-primary" />}
            title="Nothing here"
            body="No notifications match this filter."
          />
        ) : (
          <ul className="h-[calc(100dvh-140px)] divide-y divide-border overflow-y-auto">
            {filtered.map((n) => {
              const a = agentsPage?.items?.find((x) => x.id === n.agentId);
              const dept = a?.departmentId
                ? departmentsPage?.items?.find((d) => d.id === a.departmentId)
                : null;
              return (
                <li key={n.id} className="px-5 py-3">
                  <div className="flex items-start gap-3">
                    <StatusIcon status={n.status} />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="truncate text-sm font-medium">{a?.name ?? "Agent"}</span>
                        {dept && (
                          <span className="rounded-full border border-border bg-background/40 px-1.5 py-0.5 text-micro text-muted-foreground">
                            {dept.name}
                          </span>
                        )}
                        {!n.read && (
                          <span className="h-1.5 w-1.5 rounded-full bg-primary shadow-[0_0_8px_var(--primary)]" />
                        )}
                      </div>
                      <p className="mt-0.5 text-sm text-foreground/90">{n.message}</p>
                      {n.detail && <p className="mt-1 text-xs text-muted-foreground">{n.detail}</p>}
                      <div className="mt-1.5 flex items-center gap-3 text-xs text-muted-foreground">
                        <span className="inline-flex items-center gap-1">
                          <Clock className="h-3 w-3" /> {n.time}
                        </span>
                        {n.status === "warning" && (
                          <button
                            type="button"
                            onClick={() => {
                              onOpenChange(false);
                              onOpenApprovals();
                            }}
                            className="inline-flex items-center gap-1 text-primary hover:brightness-110"
                          >
                            Open in approvals <ChevronRight className="h-3 w-3" />
                          </button>
                        )}
                        <button
                          type="button"
                          onClick={() =>
                            setItems((prev) =>
                              prev.map((i) => (i.id === n.id ? { ...i, read: true } : i)),
                            )
                          }
                          className="ml-auto text-muted-foreground/70 hover:text-foreground"
                          title="Dismiss"
                        >
                          <X className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </SheetContent>
    </Sheet>
  );
}

function StatusIcon({ status }: { status: ActivityItem["status"] }) {
  const map = {
    success: { c: "var(--status-running)", Icon: CheckCircle2 },
    warning: { c: "var(--status-warning)", Icon: AlertTriangle },
    error: { c: "var(--status-error)", Icon: XCircle },
    info: { c: "var(--muted-foreground)", Icon: Info },
  }[status];
  const Icon = map.Icon;
  return (
    <div
      className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-md border"
      style={{
        color: map.c,
        borderColor: `color-mix(in oklab, ${map.c} 35%, transparent)`,
        background: `color-mix(in oklab, ${map.c} 10%, transparent)`,
      }}
    >
      <Icon className="h-3.5 w-3.5" />
    </div>
  );
}

function EmptyState({ icon, title, body }: { icon: React.ReactNode; title: string; body: string }) {
  return (
    <div className="flex h-[calc(100dvh-120px)] flex-col items-center justify-center gap-2 px-8 text-center">
      <div className="grid h-12 w-12 place-items-center rounded-full bg-primary/10">{icon}</div>
      <div className="font-serif text-lg">{title}</div>
      <p className="max-w-sm text-sm text-muted-foreground">{body}</p>
    </div>
  );
}

// ============= Shared open helpers =============

// `oc8:open-approvals` went with the sheet. Approvals live at a URL now, so the
// way to send somebody to them is a <Link to="/workspace">, not an event that
// only works if the shell happens to be listening.
export const OPEN_NOTIFICATIONS_EVENT = "oc8:open-notifications";

export function openNotifications() {
  if (typeof window !== "undefined")
    window.dispatchEvent(new CustomEvent(OPEN_NOTIFICATIONS_EVENT));
}

/** Count of unread notifications (warning + error). */
export function notificationsCount() {
  return agents.filter((a) => a.status === "warning" || a.status === "error").length;
}
