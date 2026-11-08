import { useState } from "react";
import { toast } from "sonner";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useMay } from "@/lib/governance-hooks";
import { useSaveDashboardPreset, type WidgetInstance } from "@/lib/hooks";
import { useT } from "@/lib/i18n";

export function SavePresetDialog({
  open,
  onOpenChange,
  widgets,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  widgets: WidgetInstance[];
}) {
  const t = useT();
  const may = useMay();
  const mayManageTenantPresets = may("settings:manage");
  const savePreset = useSaveDashboardPreset();
  const [name, setName] = useState("");
  const [scope, setScope] = useState<"personal" | "tenant">("personal");

  const reset = () => {
    setName("");
    setScope("personal");
  };

  const submit = () => {
    if (!name.trim()) return;
    savePreset.mutate(
      { name: name.trim(), widgets, scope },
      {
        onSuccess: () => {
          toast.success(t("Preset saved", "Preset gespeichert"));
          reset();
          onOpenChange(false);
        },
        onError: () =>
          toast.error(t("Could not save the preset.", "Preset konnte nicht gespeichert werden.")),
      },
    );
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {t("Save current layout as preset", "Aktuelles Layout als Preset speichern")}
          </DialogTitle>
        </DialogHeader>
        <div className="grid gap-3 text-sm">
          <label className="grid gap-1.5">
            <span>{t("Name", "Name")}</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="rounded-md border border-input bg-background px-3 py-2"
              autoFocus
            />
          </label>
          <div className="grid gap-1.5">
            <span>{t("Visibility", "Sichtbarkeit")}</span>
            <label className="flex items-center gap-2">
              <input
                type="radio"
                name="preset-scope"
                checked={scope === "personal"}
                onChange={() => setScope("personal")}
              />
              {t("Only for me", "Nur für mich")}
            </label>
            <label className="flex items-center gap-2">
              <input
                type="radio"
                name="preset-scope"
                checked={scope === "tenant"}
                disabled={!mayManageTenantPresets}
                onChange={() => setScope("tenant")}
              />
              {t("For the whole team", "Für das ganze Team")}
              {!mayManageTenantPresets && (
                <span className="text-xs text-muted-foreground">
                  {t("(requires settings:manage)", "(benötigt settings:manage)")}
                </span>
              )}
            </label>
          </div>
        </div>
        <div className="flex justify-end gap-2">
          <button
            onClick={() => onOpenChange(false)}
            className="rounded-md border border-border px-3 py-1.5 text-xs"
          >
            {t("Cancel", "Abbrechen")}
          </button>
          <button
            onClick={submit}
            disabled={savePreset.isPending || !name.trim()}
            className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {savePreset.isPending ? t("Saving…", "Wird gespeichert…") : t("Save", "Speichern")}
          </button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
