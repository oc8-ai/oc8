import { Box, Cog, Cpu } from "lucide-react";
import { useCapaIcon } from "@/lib/hooks";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

/** Mirrors `RuntimeOptionDTO` (`backend/src/oc8/schemas/dto.py`) exactly --
 * the API's camelCase field names, unchanged. `GET /runtimes` always opens
 * with the two built-in entries (sentinel string ids, no Capa row behind
 * either -- `oc8.runtime.registry.BUILTIN_IN_PROCESS_RUNTIME_REF` /
 * `BUILTIN_ISOLATED_RUNTIME_REF`), always `available`; `id` stays nullable
 * on this type only defensively, it is never actually null on the wire. */
export type RuntimeOption = {
  id: string | null;
  name: string;
  label: string;
  summary: string;
  capabilities: string[];
  isDefault: boolean;
  available: boolean;
  unavailableReason: string | null;
};

/** Bilingual UI copy for the two built-in runtime defaults (design spec §3.1):
 * their `label`/`summary` on the wire are an English-only API-level default
 * for non-browser consumers (`_default_entry` in
 * `backend/src/oc8/api/v1/runtimes.py`), not manifest data -- there is no
 * plugin behind either one for a German translation to live in. Keyed by
 * `name`, the one field on the DTO that tells the two builtins apart. */
const DEFAULT_RUNTIME_COPY: Record<string, { label: [string, string]; summary: [string, string] }> =
  {
    "oc8.agent-runtime": {
      label: ["Standard (in-process)", "Standard (In-Prozess)"],
      summary: [
        "Runs the agent in the shared OC8 process alongside the other agents on this tenant.",
        "Führt den Agenten im gemeinsamen OC8-Prozess zusammen mit den anderen Agenten dieses Mandanten aus.",
      ],
    },
    "oc8.agent-runtime-isolated": {
      label: ["Isolated (per-agent container)", "Isoliert (Container pro Agent)"],
      summary: [
        "Runs the agent in its own Docker container, isolated from the other agents on this tenant.",
        "Führt den Agenten in einem eigenen Docker-Container aus, isoliert von den anderen Agenten dieses Mandanten.",
      ],
    },
  };

/** The label/summary to render for `rt`: bilingual frontend copy for the
 * built-in default entries, the backend-supplied strings for everything
 * else (a plugin's own manifest copy, which this app has no translation
 * for). The ONE place this decision is made -- `RuntimePicker` and
 * `AgentRuntimePanel` both display a runtime's label/summary, and importing
 * this rather than re-deciding it locally is what keeps them from
 * rendering different text for the same entry. */
export function runtimeDisplayCopy(
  rt: RuntimeOption,
  t: (en: string, de: string) => string,
): { label: string; summary: string } {
  const copy = rt.isDefault ? DEFAULT_RUNTIME_COPY[rt.name] : undefined;
  if (!copy) return { label: rt.label, summary: rt.summary };
  return { label: t(...copy.label), summary: t(...copy.summary) };
}

// Built-in-default fallbacks (no Capa row, so no icon to fetch): distinct
// marks for the two ways a "Standard" runtime can execute, so they read
// apart from each other at a glance the same way plugin runtimes do.
const DEFAULT_RUNTIME_ICON: Partial<Record<string, typeof Cpu>> = {
  "oc8.agent-runtime": Cpu,
  "oc8.agent-runtime-isolated": Box,
};

/** Per-runtime icon, mirroring how CapaCard resolves one (capas.tsx):
 * the plugin's own declared icon if it has one, else a generic mark. Keyed
 * off `rt.name`, not `rt.id`: both built-ins now carry a real sentinel
 * string id (`builtin:in-process` / `builtin:isolated`, oc8.runtime.registry)
 * rather than `null`, so an id-truthiness check would (wrongly) try to fetch
 * a Capa icon for them. Neither has a Capa row -- `name` is what marks them
 * as built-in, and that never changes regardless of the id scheme. */
function RuntimeIcon({ rt }: { rt: RuntimeOption }) {
  const builtinFallback = DEFAULT_RUNTIME_ICON[rt.name];
  const { data: iconUrl } = useCapaIcon(builtinFallback ? null : rt.id);
  if (!builtinFallback && iconUrl) {
    return <img src={iconUrl} alt="" className="h-5 w-5 shrink-0 rounded object-contain" />;
  }
  const Fallback = builtinFallback ?? Cog;
  return <Fallback className="h-4 w-4 shrink-0 text-muted-foreground" />;
}

/** Shared picker for both the onboarding wizard and the agent detail page --
 * the one place the "one option vs. several" rendering rule lives, so the
 * two consumers cannot drift on it.
 *
 * With exactly one runtime available (the common case: no runtime plugin
 * installed) there is no choice to make, so this renders a static
 * informational row rather than a dropdown or a radio group of one, which
 * would ask for a decision that does not exist. With two or more, it
 * renders a radio list; an option with `available === false` is disabled
 * and shows its `unavailableReason` in place of the summary, so an
 * administrator who installed a broken plugin can still see it and learn
 * why it doesn't work instead of wondering where it went. */
export function RuntimePicker({
  value,
  onChange,
  runtimes,
  disabled,
}: {
  value: string | null;
  onChange: (value: string | null) => void;
  runtimes: RuntimeOption[];
  disabled?: boolean;
}) {
  const t = useT();

  if (runtimes.length <= 1) {
    const only = runtimes[0];
    if (!only) return null;
    const { label, summary } = runtimeDisplayCopy(only, t);
    return (
      <div className="flex items-start gap-3 rounded-lg border border-border bg-background/30 p-3.5 text-sm">
        <RuntimeIcon rt={only} />
        <div className="flex-1">
          <div className="font-medium">{label}</div>
          <div className="mt-0.5 text-xs text-muted-foreground">{summary}</div>
          <div className="mt-1.5 text-xs text-muted-foreground">
            {t(
              "More runtimes appear here once installed as plugins.",
              "Weitere Runtimes erscheinen hier, sobald sie als Plugins installiert sind.",
            )}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div role="radiogroup" className="grid gap-2">
      {runtimes.map((rt) => {
        const checked = value === rt.id;
        const itemDisabled = disabled || !rt.available;
        const { label, summary } = runtimeDisplayCopy(rt, t);
        return (
          <label
            key={rt.id ?? "__default__"}
            className={cn(
              "flex items-start gap-3 rounded-lg border p-3.5 text-sm transition",
              itemDisabled
                ? "cursor-not-allowed border-border bg-background/20 opacity-60"
                : checked
                  ? "cursor-pointer border-primary bg-primary/10 glow-teal"
                  : "cursor-pointer border-border bg-background/30 hover:border-primary/40",
            )}
          >
            <input
              type="radio"
              role="radio"
              name="runtime"
              className="mt-1 accent-[color:var(--primary)]"
              checked={checked}
              disabled={itemDisabled}
              onChange={() => onChange(rt.id)}
            />
            <RuntimeIcon rt={rt} />
            <div className="flex-1">
              <div className="font-medium">{label}</div>
              <div className="mt-0.5 text-xs text-muted-foreground">
                {rt.available ? summary : rt.unavailableReason}
              </div>
              {rt.capabilities.length > 0 && (
                <div className="mt-1.5 flex flex-wrap gap-1">
                  {rt.capabilities.map((cap) => (
                    <span
                      key={cap}
                      className="rounded-full border border-border bg-background/40 px-1.5 py-0.5 text-micro text-muted-foreground"
                    >
                      {cap}
                    </span>
                  ))}
                </div>
              )}
            </div>
          </label>
        );
      })}
    </div>
  );
}
