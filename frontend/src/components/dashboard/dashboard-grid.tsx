// Android-homescreen-style: a snapping, 12-column grid with collision
// handling, wrapping react-grid-layout's legacy (v1-compatible) flat-props
// API. WidthProvider measures the container with a ResizeObserver (see
// vitest.setup.ts's stub for jsdom).
//
// react-grid-layout@2.2.4 dropped the flat-props `ReactGridLayout` and the
// `WidthProvider` HOC from its main entry point in favor of a new
// composable hooks API (`react-grid-layout`'s `ReactGridLayout` there is a
// different, non-legacy component) -- both are re-exported from the
// `react-grid-layout/legacy` subpath instead, which is what this file uses.
import { ReactGridLayout, WidthProvider, type Layout } from "react-grid-layout/legacy";
import { WidgetFrame } from "@/components/dashboard/widget-frame";
import type { WidgetInstance } from "@/lib/hooks";
import "react-grid-layout/css/styles.css";

const GridLayout = WidthProvider(ReactGridLayout);

export function DashboardGrid({
  widgets,
  editing,
  onChange,
}: {
  widgets: WidgetInstance[];
  editing: boolean;
  onChange: (widgets: WidgetInstance[]) => void;
}) {
  const layout: Layout = widgets.map((w) => ({ i: w.id, x: w.x, y: w.y, w: w.w, h: w.h }));

  function handleLayoutChange(next: Layout) {
    const byId = new Map(next.map((item) => [item.i, item]));
    onChange(
      widgets.map((w) => {
        const item = byId.get(w.id);
        return item ? { ...w, x: item.x, y: item.y, w: item.w, h: item.h } : w;
      }),
    );
  }

  function updateConfig(id: string, config: Record<string, unknown>) {
    onChange(widgets.map((w) => (w.id === id ? { ...w, config } : w)));
  }

  function remove(id: string) {
    onChange(widgets.filter((w) => w.id !== id));
  }

  return (
    <GridLayout
      cols={12}
      rowHeight={40}
      containerPadding={[0, 0]}
      isDraggable={editing}
      isResizable={editing}
      layout={layout}
      onLayoutChange={handleLayoutChange}
      draggableHandle=".dashboard-widget-drag-handle"
      compactType="vertical"
    >
      {widgets.map((w) => (
        <div key={w.id}>
          <WidgetFrame
            instance={w}
            editing={editing}
            onConfigChange={(config) => updateConfig(w.id, config)}
            onRemove={() => remove(w.id)}
          />
        </div>
      ))}
    </GridLayout>
  );
}
