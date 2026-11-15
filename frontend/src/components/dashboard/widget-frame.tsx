import { Component, type ReactNode } from "react";
import { GripVertical, X } from "lucide-react";
import { WIDGET_REGISTRY } from "@/components/dashboard/widget-registry";
import type { WidgetInstance } from "@/lib/hooks";
import { useT } from "@/lib/i18n";

// A plain function so it can call `useT()` -- the class boundary below
// can't use hooks itself, but it can render this from its `render()`.
function WidgetErrorFallback() {
  const t = useT();
  return (
    <div className="flex h-full items-center justify-center p-3 text-center text-xs text-muted-foreground">
      {t("This widget could not be displayed.", "Dieses Widget konnte nicht angezeigt werden.")}
    </div>
  );
}

class WidgetErrorBoundary extends Component<{ children: ReactNode }, { hasError: boolean }> {
  state = { hasError: false };

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  render() {
    if (this.state.hasError) {
      return <WidgetErrorFallback />;
    }
    return this.props.children;
  }
}

export function WidgetFrame({
  instance,
  editing,
  onConfigChange,
  onRemove,
}: {
  instance: WidgetInstance;
  editing: boolean;
  onConfigChange: (config: Record<string, unknown>) => void;
  onRemove: () => void;
}) {
  const t = useT();
  const entry = Object.prototype.hasOwnProperty.call(WIDGET_REGISTRY, instance.type)
    ? WIDGET_REGISTRY[instance.type]
    : undefined;
  const Widget = entry?.component;

  return (
    <div className="flex h-full flex-col overflow-hidden rounded-lg border border-border bg-panel">
      {editing && (
        <div className="dashboard-widget-drag-handle flex shrink-0 cursor-move items-center gap-1.5 border-b border-border px-2 py-1">
          <GripVertical className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          <span className="flex-1" />
          {entry?.menu?.(instance.config)}
          <button
            type="button"
            onClick={onRemove}
            aria-label={t("Remove widget", "Widget entfernen")}
            className="grid h-6 w-6 shrink-0 place-items-center rounded text-muted-foreground transition hover:bg-muted/40 hover:text-foreground"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}
      <div className="flex-1 overflow-hidden">
        {Widget ? (
          <WidgetErrorBoundary>
            <Widget config={instance.config} onConfigChange={onConfigChange} />
          </WidgetErrorBoundary>
        ) : (
          <div className="flex h-full items-center justify-center p-3 text-center text-xs text-muted-foreground">
            {t("Unknown widget type", "Unbekannter Widget-Typ")}
          </div>
        )}
      </div>
    </div>
  );
}
