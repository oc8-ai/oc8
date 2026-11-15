import { WIDGET_REGISTRY } from "@/components/dashboard/widget-registry";
import { cn } from "@/lib/utils";
import type { WidgetInstance } from "@/lib/hooks";

const GRID_COLUMNS = 12;
// Two stored rows (35 px each plus the 10 px gap between them) make one old
// 80 px row exactly (dashboard-grid.tsx), so halve them to keep the sketch
// proportioned as it was.
const ROW_UNITS_PER_OLD_ROW = 2;

/** A small proportional sketch of a widget arrangement -- one rectangle per
 * tile, positioned/sized from its grid coordinates, with the widget type's
 * icon centered inside. Purely visual: renders from `WidgetInstance[]`
 * coordinates directly, never mounts a widget's actual (data-fetching)
 * component. */
export function LayoutPreview({
  widgets,
  className,
}: {
  widgets: WidgetInstance[];
  className?: string;
}) {
  const rows = widgets.length === 0 ? 1 : Math.max(...widgets.map((w) => w.y + w.h));

  return (
    <div
      className={cn("relative w-full overflow-hidden rounded-md border bg-muted/40", className)}
      style={{ aspectRatio: `${GRID_COLUMNS} / ${Math.max(rows / ROW_UNITS_PER_OLD_ROW, 4)}` }}
    >
      {widgets.map((widget) => {
        const Icon = WIDGET_REGISTRY[widget.type as keyof typeof WIDGET_REGISTRY]?.icon;
        return (
          <div
            key={widget.id}
            className="absolute flex items-center justify-center rounded-sm border border-border bg-background"
            style={{
              left: `${(widget.x / GRID_COLUMNS) * 100}%`,
              top: `${(widget.y / rows) * 100}%`,
              width: `${(widget.w / GRID_COLUMNS) * 100}%`,
              height: `${(widget.h / rows) * 100}%`,
              padding: "2px",
            }}
          >
            {Icon ? <Icon className="size-1/3 min-h-3 min-w-3 text-muted-foreground" /> : null}
          </div>
        );
      })}
    </div>
  );
}
