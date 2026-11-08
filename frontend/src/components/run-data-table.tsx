// Renders the `data_table` component (oc8.agent.components.DataTableProps).
// Props arrive as Record<string, unknown> off the wire -- this is the ONE
// place they are trusted into a concrete shape, and only after the backend's
// own pydantic validation (control_tools.py's render_component branch)
// already accepted them. Mirrors run-record-card.tsx's asFields() pattern.
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

interface DataTableColumn {
  key: string;
  label: string;
}

function asColumns(v: unknown): DataTableColumn[] {
  if (!Array.isArray(v)) return [];
  return v.filter(
    (c): c is DataTableColumn =>
      typeof c === "object" &&
      c !== null &&
      typeof (c as DataTableColumn).key === "string" &&
      typeof (c as DataTableColumn).label === "string",
  );
}

function asRows(v: unknown): Array<Record<string, string>> {
  if (!Array.isArray(v)) return [];
  return v.filter((r): r is Record<string, string> => typeof r === "object" && r !== null);
}

export function DataTable({ props }: { props: Record<string, unknown> }) {
  const title = typeof props.title === "string" ? props.title : "";
  const caption = typeof props.caption === "string" ? props.caption : null;
  const columns = asColumns(props.columns);
  const rows = asRows(props.rows);

  return (
    <section className="rounded-lg border border-border bg-background/40 p-3 text-sm">
      {title && <div className="mb-2 font-medium">{title}</div>}
      <div className="overflow-x-auto">
        <Table className="border-collapse text-xs">
          <TableHeader>
            <TableRow className="border-b border-border text-left text-muted-foreground">
              {columns.map((c) => (
                <TableHead key={c.key} className="whitespace-nowrap px-2 py-1.5 micro-label">
                  {c.label}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row, i) => (
              <TableRow key={i} className="border-b border-border/50 last:border-0">
                {columns.map((c) => (
                  <TableCell key={c.key} className="whitespace-nowrap px-2 py-1.5">
                    {row[c.key] ?? ""}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      {caption && <div className="mt-2 text-xs text-muted-foreground">{caption}</div>}
    </section>
  );
}
