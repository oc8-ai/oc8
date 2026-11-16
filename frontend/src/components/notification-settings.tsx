import { toast } from "sonner";

import { Switch } from "@/components/ui/switch";
import { useT } from "@/lib/i18n";
import {
  NOTIFICATION_KINDS,
  type NotificationKind,
  type NotificationSetting,
  useNotificationSettings,
  useSaveNotificationSettings,
} from "@/lib/notifications-api";

export function NotificationSettings() {
  const t = useT();
  const { data } = useNotificationSettings();
  const save = useSaveNotificationSettings();

  const labels: Record<NotificationKind, string> = {
    "board.available": t("New dashboards", "Neue Dashboards"),
    "run.failed": t("Failed agents", "Fehlgeschlagene Agents"),
    "approval.decided": t("Decided approvals", "Entschiedene Freigaben"),
  };

  const current = (kind: NotificationKind): NotificationSetting =>
    (Array.isArray(data) ? data : []).find((s) => s.kind === kind) ?? {
      kind,
      pushEnabled: true,
      messengerEnabled: true,
    };

  const change = (
    kind: NotificationKind,
    field: "pushEnabled" | "messengerEnabled",
    value: boolean,
  ) => {
    save.mutate(
      NOTIFICATION_KINDS.map((k) => (k === kind ? { ...current(k), [field]: value } : current(k))),
      {
        onError: () =>
          toast.error(
            t(
              "Could not save your notification settings",
              "Deine Benachrichtigungseinstellungen konnten nicht gespeichert werden",
            ),
          ),
      },
    );
  };

  return (
    <div className="space-y-2">
      {NOTIFICATION_KINDS.map((kind) => {
        const s = current(kind);
        return (
          <div key={kind} className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
            <div className="text-sm text-foreground">{labels[kind]}</div>
            <div className="flex items-center gap-4 text-xs text-muted-foreground">
              <label className="flex items-center gap-1.5">
                {t("Push", "Push")}
                <Switch
                  aria-label={`${labels[kind]}: Push`}
                  checked={s.pushEnabled}
                  disabled={!data || save.isPending}
                  onCheckedChange={(v) => change(kind, "pushEnabled", v)}
                />
              </label>
              <label className="flex items-center gap-1.5">
                {t("Messenger", "Messenger")}
                <Switch
                  aria-label={`${labels[kind]}: Messenger`}
                  checked={s.messengerEnabled}
                  disabled={!data || save.isPending}
                  onCheckedChange={(v) => change(kind, "messengerEnabled", v)}
                />
              </label>
            </div>
          </div>
        );
      })}
    </div>
  );
}
