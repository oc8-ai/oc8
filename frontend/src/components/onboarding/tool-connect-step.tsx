import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { resolveTranslation, useLang, useT } from "@/lib/i18n";
import {
  useAvailablePlugins,
  useEnablePlugin,
  useInstallPluginFromDisk,
  useTestMcpConnectionById,
  type DiscoveredCapa,
} from "@/lib/hooks";
import { api } from "@/lib/api";
import { PluginSetupFields } from "@/components/plugin-setup-fields";

// Mirrors the un-exported `keys.mcp` query key in `@/lib/hooks` (`["mcp",
// "connections"]`) -- that key isn't exported, so it's duplicated here
// rather than reaching into a module another concurrent change might be
// touching. Keep in sync with `hooks.ts` if that key ever changes shape.
const MCP_CONNECTIONS_KEY = ["mcp", "connections"] as const;

/** Adapts the existing plugin-install/enable/configure flow into a wizard
 * step. Unlike the Capas page's `CapaSetupDialog`, this renders
 * `PluginSetupFields` inline and surfaces the resulting MCP `connectionId`
 * through `onDone` so the Guardrails step can act on it. A non-MCP setup
 * plugin has no connection to surface, so `onDone` is called with `null` --
 * callers must treat that as "configured, but skip Guardrails". */
export function ToolConnectStep({
  onDone,
  onSkip,
}: {
  onDone: (connectionId: string | null) => void;
  onSkip: () => void;
}) {
  const t = useT();
  const { lang } = useLang();
  const qc = useQueryClient();
  // Wizard picker over every discovered plugin, not a paginated list view.
  const { data: pluginsPage, isLoading } = useAvailablePlugins({ pageSize: 200 });
  const plugins = pluginsPage?.items ?? [];
  const install = useInstallPluginFromDisk();
  const enable = useEnablePlugin();
  const test = useTestMcpConnectionById();
  const [target, setTarget] = useState<DiscoveredCapa | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [busyId, setBusyId] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const candidates = plugins.filter((p) => p.type !== "department_template" && p.setup);

  // Calling `onSkip()` (which updates the wizard's own state) directly
  // during render would fire while this component is still rendering --
  // React flags that as an update-during-render anti-pattern. Deferring it
  // to an effect lets this render finish first; `candidates.length` (a
  // primitive) is used in the deps instead of `candidates` itself since the
  // array is a fresh `.filter()` result every render.
  useEffect(() => {
    if (!isLoading && candidates.length === 0) {
      onSkip();
    }
  }, [isLoading, candidates.length, onSkip]);

  if (isLoading) {
    return (
      <Panel className="p-6 text-sm text-muted-foreground">
        {t("Loading available tools…", "Verfügbare Tools werden geladen …")}
      </Panel>
    );
  }

  if (candidates.length === 0) {
    // The effect above handles `onSkip()`; render nothing while it fires.
    return null;
  }

  const connect = async (plugin: DiscoveredCapa) => {
    setBusyId(plugin.pluginId);
    try {
      let current = plugin;
      if (!current.installed) {
        await install.mutateAsync(current.pluginId);
        // `install.mutateAsync` resolves to `{id, name, semver}`, not a full
        // `DiscoveredCapa` -- re-fetch through the same `api` client every
        // other call in this codebase uses (raw `fetch` would skip the Bearer
        // auth header `src/lib/api.ts` attaches).
        const refreshed = await api.get<DiscoveredCapa[]>("/capas/available");
        current = refreshed.find((p) => p.pluginId === current.pluginId) ?? current;
      }
      const databaseId = current.databaseId;
      if (current.installed && current.installationStatus !== "enabled" && databaseId) {
        await enable.mutateAsync({ pluginId: databaseId, grantedPermissions: current.permissions });
      }
      setTarget(current);
      setValues(
        Object.fromEntries((current.setup?.fields ?? []).map((f) => [f.key, f.default ?? ""])),
      );
    } catch (err) {
      toast.error(t("Could not connect tool", "Tool konnte nicht verbunden werden"), {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusyId(null);
    }
  };

  const submitSetup = async () => {
    if (!target?.setup || !target.databaseId) return;
    const missing = target.setup.fields.find(
      (field) => field.required && !(values[field.key] ?? field.default ?? "").trim(),
    );
    if (missing) {
      toast.error(t(`${missing.label} is required.`, `${missing.label} ist erforderlich.`));
      return;
    }
    setSubmitting(true);
    try {
      const result = await api.post<{ connectionId: string | null }>(
        `/capas/${target.databaseId}/setup`,
        { values },
      );
      if (target.setup.mcp && result.connectionId) {
        const tested = await test.mutateAsync(result.connectionId);
        if (!tested.connected) {
          const detail =
            typeof tested.health?.error === "string" ? tested.health.error : "unknown error";
          throw new Error(`Connection test failed: ${detail}`);
        }
      }
      if (result.connectionId) {
        // `api.post` here bypasses `useConfigurePlugin`, which would
        // otherwise invalidate this cache key on success -- do it manually
        // so any already-mounted view reading MCP connections doesn't show
        // stale data after a successful setup.
        qc.invalidateQueries({ queryKey: MCP_CONNECTIONS_KEY });
      }
      toast.success(t(`${target.name} configured.`, `${target.name} konfiguriert.`));
      // A non-MCP setup plugin has nothing for Guardrails to gate -- there's
      // no MCP connection to attach write/send/approval flags to -- so a
      // successful configure must still advance the wizard, just with no id.
      onDone(result.connectionId);
    } catch (err) {
      toast.error(t("Plugin setup failed.", "Tool-Einrichtung fehlgeschlagen."), {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setSubmitting(false);
    }
  };

  if (target?.setup) {
    return (
      <Panel className="space-y-4 p-6">
        <h3 className="font-serif text-lg">
          {resolveTranslation(target.setup.title, target.setup.translations?.title, lang)}
        </h3>
        <PluginSetupFields setup={target.setup} values={values} onChange={setValues} />
        <button
          onClick={submitSetup}
          disabled={submitting || test.isPending}
          className="w-full rounded-md bg-brand-gradient px-4 py-2 text-sm font-medium text-brand-gradient-foreground transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {submitting || test.isPending
            ? t("Connecting…", "Wird verbunden …")
            : resolveTranslation(
                target.setup.submit_label,
                target.setup.translations?.submit_label,
                lang,
              )}
        </button>
      </Panel>
    );
  }

  return (
    <Panel className="space-y-3 p-6">
      {candidates.map((p) => (
        <button
          key={p.pluginId}
          onClick={() => connect(p)}
          disabled={busyId === p.pluginId}
          className="flex w-full items-center justify-between rounded-md border border-border bg-background/30 px-4 py-3 text-left text-sm transition hover:border-primary/40 disabled:opacity-60"
        >
          <span>{p.name}</span>
          <span className="text-xs text-primary">
            {busyId === p.pluginId
              ? t("Connecting…", "Wird verbunden …")
              : t("Connect", "Verbinden")}
          </span>
        </button>
      ))}
    </Panel>
  );
}
