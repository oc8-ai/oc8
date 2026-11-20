import { Plus } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { WIDGET_REGISTRY } from "@/components/dashboard/widget-registry";
import { useBoards } from "@/lib/boards-api";
import type { WidgetType } from "@/lib/hooks";
import { useT } from "@/lib/i18n";

export function WidgetPicker({
  onAdd,
}: {
  onAdd: (type: WidgetType, config?: Record<string, unknown>) => void;
}) {
  const t = useT();
  const de = t("en", "de") === "de";
  const { data: boards } = useBoards();
  const types = (Object.keys(WIDGET_REGISTRY) as WidgetType[]).filter(
    (type) => WIDGET_REGISTRY[type].pickable !== false,
  );
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={t("Add widget", "Widget hinzufügen")}
          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-panel px-3 py-2 text-sm transition hover:text-primary"
        >
          <Plus className="h-4 w-4" /> {t("Add widget", "Widget hinzufügen")}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {types.map((type) => (
          <DropdownMenuItem key={type} onClick={() => onAdd(type)}>
            {WIDGET_REGISTRY[type].label(de)}
          </DropdownMenuItem>
        ))}
        {boards?.length ? (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuLabel>{t("Dashboards", "Dashboards")}</DropdownMenuLabel>
            {boards.map((board) => (
              <DropdownMenuItem
                key={board.id}
                onClick={() => onAdd("data_board", { boardId: board.id })}
              >
                <span className="min-w-0 truncate">{board.title}</span>
              </DropdownMenuItem>
            ))}
          </>
        ) : null}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
