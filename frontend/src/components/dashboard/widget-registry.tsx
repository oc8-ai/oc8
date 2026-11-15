// Same shape and spirit as RUN_COMPONENT_REGISTRY (run-record-card.tsx): a
// short, compiled lookup table, not a plugin system. A WidgetInstance whose
// `type` is absent from this table (a future/removed type) is handled by
// DashboardGrid's own fallback -- this registry itself only ever holds the
// seven current types.
import {
  Activity,
  CheckCircle2,
  Inbox,
  Kanban,
  MessageSquare,
  PieChart,
  Wallet,
} from "lucide-react";
import type { ReactElement, ReactNode } from "react";
import { ActivityWidget } from "@/components/dashboard/widgets/activity-widget";
import { ApprovalsWidget } from "@/components/dashboard/widgets/approvals-widget";
import { BudgetWidget } from "@/components/dashboard/widgets/budget-widget";
import { ChatWidget } from "@/components/dashboard/widgets/chat-widget";
import { NeedsMeWidget } from "@/components/dashboard/widgets/needs-me-widget";
import { ReportsWidget } from "@/components/dashboard/widgets/reports-widget";
import { TaskBoardWidget } from "@/components/dashboard/widgets/task-board-widget";
import { WIDGET_LABELS } from "@/components/dashboard/widget-labels";
import type { WidgetType } from "@/lib/hooks";

type WidgetComponentProps = {
  config: Record<string, unknown>;
  onConfigChange: (config: Record<string, unknown>) => void;
};

export const WIDGET_REGISTRY: Record<
  WidgetType,
  {
    label: (de: boolean) => string;
    component: (props: WidgetComponentProps) => ReactElement;
    defaultSize: { w: number; h: number };
    /** Small glyph shown in the layout sketch and widget picker -- purely
     * decorative, keep separate from `component` so the preview can render
     * without mounting a widget's (potentially data-fetching) full body. */
    icon: typeof MessageSquare;
    /** Extra edit-mode controls drawn in the frame header next to the remove
     * button; none of the built-in widgets uses it. */
    menu?: (config: Record<string, unknown>) => ReactNode;
  }
> = {
  chat: {
    label: (de) => (de ? WIDGET_LABELS["chat"].de : WIDGET_LABELS["chat"].en),
    component: ChatWidget,
    defaultSize: { w: 6, h: 12 },
    icon: MessageSquare,
  },
  approvals: {
    label: (de) => (de ? WIDGET_LABELS["approvals"].de : WIDGET_LABELS["approvals"].en),
    component: ApprovalsWidget,
    defaultSize: { w: 4, h: 10 },
    icon: CheckCircle2,
  },
  "needs-me": {
    label: (de) => (de ? WIDGET_LABELS["needs-me"].de : WIDGET_LABELS["needs-me"].en),
    component: NeedsMeWidget,
    defaultSize: { w: 4, h: 12 },
    icon: Inbox,
  },
  reports: {
    label: (de) => (de ? WIDGET_LABELS["reports"].de : WIDGET_LABELS["reports"].en),
    component: ReportsWidget,
    defaultSize: { w: 4, h: 8 },
    icon: PieChart,
  },
  budget: {
    label: (de) => (de ? WIDGET_LABELS["budget"].de : WIDGET_LABELS["budget"].en),
    component: BudgetWidget,
    defaultSize: { w: 3, h: 6 },
    icon: Wallet,
  },
  activity: {
    label: (de) => (de ? WIDGET_LABELS["activity"].de : WIDGET_LABELS["activity"].en),
    component: ActivityWidget,
    defaultSize: { w: 3, h: 8 },
    icon: Activity,
  },
  tasks: {
    label: (de) => (de ? WIDGET_LABELS["tasks"].de : WIDGET_LABELS["tasks"].en),
    component: TaskBoardWidget,
    defaultSize: { w: 6, h: 10 },
    icon: Kanban,
  },
};
