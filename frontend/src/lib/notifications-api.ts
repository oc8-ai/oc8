import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { useLang } from "@/lib/i18n";

export type NotificationKind = "board.available" | "run.failed" | "approval.decided";

export const NOTIFICATION_KINDS: NotificationKind[] = [
  "board.available",
  "run.failed",
  "approval.decided",
];

interface LocalizedText {
  en: string;
  de: string;
}

/** A notification as the API returns it: both languages travel. */
export interface NotificationDTO {
  id: string;
  kind: string;
  subjectId: string | null;
  title: LocalizedText;
  body: LocalizedText;
  link: string;
  createdAt: string;
  read: boolean;
}

/** A notification rendered in the current UI language. */
export interface Notification {
  id: string;
  kind: string;
  subjectId: string | null;
  title: string;
  body: string;
  link: string;
  createdAt: string;
  read: boolean;
}

export interface NotificationPageDTO {
  items: NotificationDTO[];
  totalCount: number;
  nextCursor: string | null;
}

export interface NotificationSetting {
  kind: NotificationKind;
  pushEnabled: boolean;
  messengerEnabled: boolean;
}

export interface NotificationFilter {
  unread?: boolean;
  kind?: string;
}

export const notificationKeys = {
  all: ["notifications"] as const,
  list: (filter: NotificationFilter) => ["notifications", "list", filter] as const,
  unreadCount: () => ["notifications", "unread-count"] as const,
  settings: () => ["notifications", "settings"] as const,
};

function localize(n: NotificationDTO, lang: string): Notification {
  const pick = (t: LocalizedText) => (lang === "de" ? t.de : t.en);
  return { ...n, title: pick(n.title), body: pick(n.body) };
}

export function useNotifications(filter: NotificationFilter = {}) {
  const { lang } = useLang();
  return useQuery({
    queryKey: notificationKeys.list(filter),
    queryFn: () => {
      const params = new URLSearchParams();
      if (filter.unread !== undefined) params.set("unread", String(filter.unread));
      if (filter.kind) params.set("kind", filter.kind);
      const qs = params.toString();
      return api.get<NotificationPageDTO>(`/notifications${qs ? `?${qs}` : ""}`);
    },
    select: (page): Notification[] => page.items.map((n) => localize(n, lang)),
  });
}

export function useUnreadCount() {
  return useQuery({
    queryKey: notificationKeys.unreadCount(),
    queryFn: () => api.get<{ count: number }>("/notifications/unread-count"),
    select: (d) => d.count,
  });
}

export function useMarkRead() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<void>(`/notifications/${encodeURIComponent(id)}/read`),
    onSuccess: () => qc.invalidateQueries({ queryKey: notificationKeys.all }),
  });
}

export function useMarkAllRead() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<void>("/notifications/read-all"),
    onSuccess: () => qc.invalidateQueries({ queryKey: notificationKeys.all }),
  });
}

export function useNotificationSettings() {
  return useQuery({
    queryKey: notificationKeys.settings(),
    queryFn: () => api.get<NotificationSetting[]>("/notifications/settings"),
  });
}

export function useSaveNotificationSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (settings: NotificationSetting[]) =>
      api.put<NotificationSetting[]>("/notifications/settings", settings),
    onSuccess: (saved, sent) => {
      qc.setQueryData(notificationKeys.settings(), saved ?? sent);
    },
  });
}
