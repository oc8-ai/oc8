import { useNavigate } from "@tanstack/react-router";
import { AlertTriangle, Bell, CheckCircle2, LayoutDashboard } from "lucide-react";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useT } from "@/lib/i18n";
import {
  type Notification,
  useMarkAllRead,
  useMarkRead,
  useNotifications,
  useUnreadCount,
} from "@/lib/notifications-api";

const KIND_ICON = {
  "board.available": LayoutDashboard,
  "run.failed": AlertTriangle,
  "approval.decided": CheckCircle2,
} as const;

function relativeTime(iso: string, t: ReturnType<typeof useT>): string {
  const minutes = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 60000));
  if (minutes < 1) return t("just now", "gerade eben");
  if (minutes < 60) return t(`${minutes} min ago`, `vor ${minutes} Min.`);
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return t(`${hours} h ago`, `vor ${hours} Std.`);
  const days = Math.floor(hours / 24);
  return t(`${days} d ago`, `vor ${days} T.`);
}

export function NotificationBell() {
  const t = useT();
  const navigate = useNavigate();
  const { data: count = 0 } = useUnreadCount();
  const { data: items = [] } = useNotifications();
  const markRead = useMarkRead();
  const markAll = useMarkAllRead();

  const open = (n: Notification) => {
    if (!n.read) markRead.mutate(n.id);
    if (n.link) navigate({ href: n.link });
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        aria-label={t("Notifications", "Benachrichtigungen")}
        className="relative ml-1 inline-flex h-7 w-7 items-center justify-center rounded-md border border-border bg-background/40 text-muted-foreground transition hover:text-foreground focus:outline-none"
      >
        <Bell className="h-3.5 w-3.5" />
        {count > 0 && (
          <span className="absolute -right-1.5 -top-1.5 grid h-4 min-w-4 place-items-center rounded-full bg-primary px-1 text-[9px] font-semibold leading-none text-primary-foreground">
            {count > 9 ? "9+" : count}
          </span>
        )}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-[min(22rem,calc(100vw-2rem))]">
        <div className="flex items-center justify-between px-2 py-1.5">
          <span className="text-xs font-semibold">{t("Notifications", "Benachrichtigungen")}</span>
          <button
            type="button"
            onClick={() => markAll.mutate()}
            className="text-[11px] text-muted-foreground hover:text-foreground"
          >
            {t("Mark all as read", "Alle als gelesen markieren")}
          </button>
        </div>
        <DropdownMenuSeparator />
        {items.length === 0 ? (
          <div className="px-2 py-6 text-center text-xs text-muted-foreground">
            {t("No notifications", "Keine Benachrichtigungen")}
          </div>
        ) : (
          <div className="max-h-96 overflow-y-auto">
            {items.map((n) => {
              const Icon = KIND_ICON[n.kind as keyof typeof KIND_ICON] ?? Bell;
              return (
                <DropdownMenuItem
                  key={n.id}
                  onSelect={() => open(n)}
                  className="items-start gap-2 whitespace-normal"
                >
                  <Icon className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                  <div className="min-w-0 flex-1">
                    <div className="text-xs font-medium">{n.title}</div>
                    <div className="text-xs text-muted-foreground">{n.body}</div>
                    <div className="mt-0.5 text-[10px] text-muted-foreground">
                      {relativeTime(n.createdAt, t)}
                    </div>
                  </div>
                  {!n.read && (
                    <span
                      aria-label={t("Unread", "Ungelesen")}
                      className="mt-1 h-2 w-2 shrink-0 rounded-full bg-primary"
                    />
                  )}
                </DropdownMenuItem>
              );
            })}
          </div>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
