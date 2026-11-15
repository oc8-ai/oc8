import { useT } from "@/lib/i18n";
import type { WidgetType } from "@/lib/hooks";

/** The one source of each widget type's name: the registry label and the
 * widget's own title both read it. A separate module so widgets can use it
 * without importing the registry (which imports the widgets). */
export const WIDGET_LABELS: Record<WidgetType, { en: string; de: string }> = {
  chat: { en: "Chat", de: "Chat" },
  approvals: { en: "Approvals", de: "Freigaben" },
  "needs-me": { en: "Waiting on me", de: "Wartet auf mich" },
  reports: { en: "Reports", de: "Reports" },
  budget: { en: "Budget", de: "Budget" },
  activity: { en: "Activity", de: "Aktivität" },
  tasks: { en: "Task Board", de: "Aufgaben-Board" },
};

/** A widget type's registry name in the current language. */
export function useWidgetLabel(type: WidgetType): string {
  const t = useT();
  const labels = WIDGET_LABELS[type];
  return t(labels.en, labels.de);
}
