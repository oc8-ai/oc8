// Settings: "Run scripts in previews". The chat shows an answer's HTML
// artifacts in a sandboxed frame; turned off, every preview is static.
import { toast } from "sonner";

import { Panel } from "@/components/app-shell";
import { Switch } from "@/components/ui/switch";
import { useMay } from "@/lib/governance-hooks";
import { useT } from "@/lib/i18n";
import { usePreviewSettings, useUpdatePreviewSettings } from "@/lib/preview-settings";

export function PreviewScriptsSetting() {
  const t = useT();
  const may = useMay();
  const { data } = usePreviewSettings();
  const update = useUpdatePreviewSettings();
  const canManage = may("settings:manage");

  async function save(runScripts: boolean) {
    try {
      await update.mutateAsync({ runScripts });
      toast.success(t("Preview setting saved", "Vorschau-Einstellung gespeichert"));
    } catch {
      toast.error(
        t(
          "Could not save the preview setting.",
          "Vorschau-Einstellung konnte nicht gespeichert werden.",
        ),
      );
    }
  }

  return (
    <Panel id="previews" className="p-5">
      <h3 className="font-serif text-lg">{t("Chat previews", "Vorschauen im Chat")}</h3>
      <div className="mt-3 flex items-center gap-3">
        <Switch
          aria-label={t("Run scripts in previews", "Skripte in Vorschauen ausführen")}
          checked={data?.runScripts ?? true}
          disabled={!canManage || !data || update.isPending}
          onCheckedChange={(runScripts) => void save(runScripts)}
        />
        <span className="text-sm">
          {t("Run scripts in previews", "Skripte in Vorschauen ausführen")}
        </span>
      </div>
      <p className="mt-2 text-xs text-muted-foreground">
        {t(
          "Pages, apps and diagrams the assistant writes open beside the chat in a sandbox without network access. Turned off, they are shown without running their scripts.",
          "Seiten, Apps und Diagramme, die der Assistent schreibt, öffnen sich neben dem Chat in einer Sandbox ohne Netzwerkzugriff. Ausgeschaltet werden sie angezeigt, ohne ihre Skripte auszuführen.",
        )}
      </p>
    </Panel>
  );
}
