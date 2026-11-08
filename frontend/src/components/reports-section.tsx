// "My work"'s Reports section: finished runs that rendered at least one
// chart/table/record card -- what a daily "give me a financial report"
// agent actually produces, surfaced somewhere besides its own run's Live
// Log. Read-only: there is nothing to decide here, unlike the queue above it.

import { FileBarChart } from "lucide-react";
import { Panel } from "@/components/app-shell";
import { RUN_COMPONENT_REGISTRY } from "@/components/run-record-card";
import { useReports } from "@/lib/hooks";
import { useT } from "@/lib/i18n";

export function ReportsSection() {
  const t = useT();
  const { data: reports, isLoading } = useReports();

  if (isLoading) return null;
  if (!reports || reports.length === 0) return null;

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <FileBarChart className="h-4 w-4 text-muted-foreground" />
        <h2 className="font-serif text-xl">{t("Reports", "Berichte")}</h2>
      </div>
      <div className="space-y-3">
        {reports.map((report) => (
          <Panel key={report.runId} className="p-3">
            <div className="mb-2 flex items-center justify-between text-xs text-muted-foreground">
              <span className="font-medium text-foreground">{report.agentName}</span>
              <span>{new Date(report.createdAt).toLocaleString()}</span>
            </div>
            <div className="space-y-2">
              {report.renderedComponents.map((c, i) => {
                const Renderer = Object.prototype.hasOwnProperty.call(
                  RUN_COMPONENT_REGISTRY,
                  c.componentKey,
                )
                  ? RUN_COMPONENT_REGISTRY[c.componentKey]
                  : undefined;
                return Renderer ? <Renderer key={i} props={c.props} /> : null;
              })}
            </div>
          </Panel>
        ))}
      </div>
    </div>
  );
}
