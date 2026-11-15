import { ReportsSection } from "@/components/reports-section";
import { WidgetTitle } from "@/components/dashboard/widget-title";
import { useT } from "@/lib/i18n";

export function ReportsWidget(_props: {
  config: Record<string, unknown>;
  onConfigChange: (c: Record<string, unknown>) => void;
}) {
  const t = useT();
  return (
    <div className="flex h-full flex-col">
      <WidgetTitle title={t("Reports", "Reports")} />
      <div data-testid="reports-widget-scroll" className="min-h-0 flex-1 overflow-y-auto p-2">
        <ReportsSection />
      </div>
    </div>
  );
}
