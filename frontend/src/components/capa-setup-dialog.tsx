import { Puzzle, X } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import { type DiscoveredCapa, useConfigurePlugin, useTestMcpConnectionById } from "@/lib/hooks";
import { PluginSetupFields } from "@/components/plugin-setup-fields";
import { resolveTranslation, useLang } from "@/lib/i18n";

/** Generic renderer for the declarative setup contract in a plugin manifest.
 * It contains no provider, product, command, environment-variable, or field
 * names. Those all belong to the plugin. */
export function CapaSetupDialog({
  plugin,
  onClose,
}: {
  plugin: DiscoveredCapa;
  onClose: () => void;
}) {
  const setup = plugin.setup;
  const { lang } = useLang();
  const configure = useConfigurePlugin(plugin.databaseId ?? "");
  const test = useTestMcpConnectionById();
  const defaults = useMemo(
    () =>
      Object.fromEntries((setup?.fields ?? []).map((field) => [field.key, field.default ?? ""])),
    [setup],
  );
  const [values, setValues] = useState<Record<string, string>>(defaults);

  if (!setup) return null;

  const submit = async () => {
    const missing = setup.fields.find(
      (field) => field.required && !(values[field.key] ?? field.default ?? "").trim(),
    );
    if (missing) {
      toast.error(`${missing.label} is required.`);
      return;
    }
    try {
      const result = await configure.mutateAsync(values);
      // No `mcp` block means no connection to test -- the plugin's own
      // setup_validate (if it declared one) already ran server-side, inside
      // the /setup call that just succeeded.
      if (setup.mcp && result.connectionId) {
        const tested = await test.mutateAsync(result.connectionId);
        if (!tested.connected) {
          const detail =
            typeof tested.health?.error === "string" ? tested.health.error : "unknown error";
          throw new Error(`Connection test failed: ${detail}`);
        }
      }
      toast.success(`${plugin.name} configured.`);
      onClose();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Plugin setup failed.");
    }
  };

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4" onClick={onClose}>
      <div
        className="w-full max-w-xl rounded-xl border border-border bg-panel p-5 shadow-2xl"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="flex items-start justify-between gap-4">
          <div className="flex gap-3">
            <div className="grid h-10 w-10 place-items-center rounded-lg bg-primary/15 text-primary">
              <Puzzle className="h-5 w-5" />
            </div>
            <div>
              <h3 className="font-serif text-lg">
                {resolveTranslation(setup.title, setup.translations?.title, lang)}
              </h3>
              {setup.description && (
                <p className="mt-1 text-xs text-muted-foreground">
                  {resolveTranslation(setup.description, setup.translations?.description, lang)}
                </p>
              )}
            </div>
          </div>
          <button onClick={onClose} aria-label="Close">
            <X className="h-4 w-4" />
          </button>
        </header>
        <div className="mt-5">
          <PluginSetupFields setup={setup} values={values} onChange={setValues} />
        </div>
        <footer className="mt-5 flex justify-end gap-2">
          <button onClick={onClose} className="rounded-md border border-border px-3 py-1.5 text-xs">
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={configure.isPending || test.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-40"
          >
            {resolveTranslation(setup.submit_label, setup.translations?.submit_label, lang)}
          </button>
        </footer>
      </div>
    </div>
  );
}
