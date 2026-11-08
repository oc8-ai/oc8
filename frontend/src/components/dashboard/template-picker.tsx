import { Trash2 } from "lucide-react";
import { LayoutPreview } from "@/components/dashboard/layout-preview";
import { useConfirm } from "@/hooks/use-confirm";
import { useMay } from "@/lib/governance-hooks";
import {
  useDashboardPresets,
  useDashboardTemplates,
  useDeleteDashboardPreset,
  type DashboardPresetDTO,
  type DashboardTemplateDTO,
} from "@/lib/hooks";
import { useT } from "@/lib/i18n";

export function TemplatePicker({
  onPick,
  onPickPreset,
}: {
  onPick: (template: DashboardTemplateDTO) => void;
  onPickPreset?: (preset: DashboardPresetDTO) => void;
}) {
  const t = useT();
  const de = t("en", "de") === "de";
  const { data: templates, isPending } = useDashboardTemplates();
  const { data: presets } = useDashboardPresets();
  const may = useMay();
  const mayManageTenantPresets = may("settings:manage");
  const deletePreset = useDeleteDashboardPreset();
  const { confirm, ConfirmDialog } = useConfirm();

  if (isPending) {
    return (
      <div className="flex items-center justify-center p-10 text-sm text-muted-foreground">
        {t("Loading…", "Wird geladen…")}
      </div>
    );
  }

  async function handleDeletePreset(preset: DashboardPresetDTO) {
    const ok = await confirm({
      title: t("Delete this preset?", "Dieses Preset löschen?"),
      description: t(
        `Delete "${preset.name}"? This cannot be undone.`,
        `"${preset.name}" löschen? Das kann nicht rückgängig gemacht werden.`,
      ),
      confirmLabel: t("Delete", "Löschen"),
      cancelLabel: t("Cancel", "Abbrechen"),
    });
    if (!ok) return;
    deletePreset.mutate(preset.id);
  }

  return (
    <div className="mx-auto max-w-3xl space-y-6 p-10 text-center">
      <div className="space-y-4">
        <h2 className="font-serif text-xl">
          {t("Choose a starting layout", "Wähle eine Startanordnung")}
        </h2>
        <p className="text-sm text-muted-foreground">
          {t(
            "You can rearrange, resize, or remove any tile afterwards.",
            "Du kannst danach jede Kachel verschieben, verändern oder entfernen.",
          )}
        </p>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          {(templates ?? []).map((template) => (
            <button
              key={template.id}
              type="button"
              onClick={() => onPick(template)}
              className="space-y-2 rounded-lg border border-border bg-panel p-3 text-left transition hover:border-primary/50"
            >
              <LayoutPreview widgets={template.widgets} />
              <div>
                <div className="font-medium">{de ? template.name.de : template.name.en}</div>
                <div className="mt-1 text-xs text-muted-foreground">
                  {template.widgets.length}{" "}
                  {template.widgets.length === 1 ? t("tile", "Kachel") : t("tiles", "Kacheln")}
                </div>
              </div>
            </button>
          ))}
        </div>
      </div>

      {presets && presets.length > 0 && (
        <div className="space-y-4 text-left">
          <h3 className="text-center font-serif text-lg">
            {t("Saved presets", "Gespeicherte Presets")}
          </h3>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            {presets.map((preset) => {
              const mayDelete =
                preset.mine || (preset.scope === "tenant" && mayManageTenantPresets);
              return (
                <div
                  key={preset.id}
                  className="space-y-2 rounded-lg border border-border bg-panel p-3 text-left transition hover:border-primary/50"
                >
                  <button
                    type="button"
                    onClick={() => onPickPreset?.(preset)}
                    aria-label={preset.name}
                    className="block w-full text-left"
                  >
                    <LayoutPreview widgets={preset.widgets} />
                  </button>
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <div className="font-medium">{preset.name}</div>
                      <div className="mt-1 text-xs text-muted-foreground">
                        {preset.scope === "tenant"
                          ? t("Shared with the team", "Für das ganze Team")
                          : t("Only visible to you", "Nur für dich sichtbar")}
                      </div>
                    </div>
                    {mayDelete && (
                      <button
                        type="button"
                        onClick={() => handleDeletePreset(preset)}
                        disabled={deletePreset.isPending}
                        title={t("Delete preset", "Preset löschen")}
                        className="shrink-0 rounded-md p-1.5 text-muted-foreground transition hover:text-status-error disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
      {ConfirmDialog}
    </div>
  );
}
