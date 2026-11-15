import { ReportsSection } from "@/components/reports-section";
import { useWidgetLabel } from "@/components/dashboard/widget-labels";
import { WidgetTitle } from "@/components/dashboard/widget-title";

export function ReportsWidget(_props: {
  config: Record<string, unknown>;
  onConfigChange: (c: Record<string, unknown>) => void;
}) {
  const title = useWidgetLabel("reports");
  return (
    <div className="flex h-full flex-col">
      <WidgetTitle title={title} />
      <div data-testid="reports-widget-scroll" className="min-h-0 flex-1 overflow-y-auto p-2">
        <ReportsSection />
      </div>
    </div>
  );
}
