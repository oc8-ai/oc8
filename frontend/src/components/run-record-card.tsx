import type { ReactElement } from "react";
import { DataTable } from "@/components/run-data-table";
import { BarChart, LineChart } from "@/components/run-chart";
import { FollowupCard, ResponsibilityCard } from "@/components/copilot-cards";

interface RecordCardField {
  label: string;
  value: string;
}

// Props arrive as Record<string, unknown> off the wire (RunComponentDTO.props)
// -- this is the ONE place they are trusted into a concrete shape, and only
// after the backend's own pydantic validation (control_tools.py's
// render_component branch) already accepted them.
function asFields(v: unknown): RecordCardField[] {
  if (!Array.isArray(v)) return [];
  return v.filter(
    (f): f is RecordCardField =>
      typeof f === "object" &&
      f !== null &&
      typeof (f as RecordCardField).label === "string" &&
      typeof (f as RecordCardField).value === "string",
  );
}

// Validate that a URL only uses safe schemes (http/https).
// Reject javascript:, data:, vbscript:, and other potentially dangerous schemes.
function safeLinkUrl(v: unknown): string | null {
  if (typeof v !== "string") return null;
  try {
    const u = new URL(v, window.location.origin);
    return u.protocol === "http:" || u.protocol === "https:" ? v : null;
  } catch {
    return null;
  }
}

export function RecordCard({ props }: { props: Record<string, unknown> }) {
  const title = typeof props.title === "string" ? props.title : "";
  const subtitle = typeof props.subtitle === "string" ? props.subtitle : null;
  const fields = asFields(props.fields);
  const linkLabel = typeof props.link_label === "string" ? props.link_label : null;
  const linkUrl = safeLinkUrl(props.link_url);

  return (
    <section className="rounded-lg border border-border bg-background/40 p-3 text-sm">
      <div className="font-medium">{title}</div>
      {subtitle && <div className="text-xs text-muted-foreground">{subtitle}</div>}
      {fields.length > 0 && (
        <dl className="mt-2 space-y-1">
          {fields.map((f, i) => (
            <div key={i} className="flex justify-between gap-3 text-xs">
              <dt className="text-muted-foreground">{f.label}</dt>
              <dd>{f.value}</dd>
            </div>
          ))}
        </dl>
      )}
      {linkLabel && linkUrl && (
        <a
          href={linkUrl}
          target="_blank"
          rel="noreferrer"
          className="mt-2 inline-block text-xs text-primary underline"
        >
          {linkLabel}
        </a>
      )}
    </section>
  );
}

// A fixed, compiled mapping from componentKey to renderer -- never a dynamic
// lookup into arbitrary code. A componentKey this registry does not know
// renders nothing (see the RUN_COMPONENT_REGISTRY lookup in RunTranscript,
// agents.$id.tsx), matching the backend's own COMPONENT_CATALOG
// (oc8.agent.components): the catalogue on each side is the whole trust
// boundary, and both are a short, compiled list.
export const RUN_COMPONENT_REGISTRY: Record<
  string,
  (props: { props: Record<string, unknown> }) => ReactElement
> = {
  record_card: RecordCard,
  data_table: DataTable,
  bar_chart: BarChart,
  line_chart: LineChart,
  responsibility_card: ResponsibilityCard,
  followup_card: FollowupCard,
};
