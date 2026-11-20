import { useState } from "react";
import { Plus, Search, Wrench, X } from "lucide-react";
import { toast } from "sonner";
import { CredentialPicker } from "@/components/credential-picker";
import { useT } from "@/lib/i18n";
import { useSetAgentToolAccess } from "@/lib/connections";
import type { McpConnection } from "@/lib/hooks";
import type { GuardrailValue } from "@/components/guardrail-preset-picker";

function preferredPreset(connection: McpConnection): GuardrailValue | null {
  if (connection.guardrailLibrary && connection.guardrailLibrary.length > 0) {
    const entry =
      connection.guardrailLibrary.find((e) => e.read || e.modify) ?? connection.guardrailLibrary[0];
    return {
      read: entry.read,
      modify: entry.modify,
      approvalActions: entry.approvalActions,
      approvalEur: entry.approvalEur,
      only: entry.only,
      conditions: [],
    };
  }
  const recommended =
    connection.guardrailPresets.find((p) => p.recommended) ?? connection.guardrailPresets[0];
  if (!recommended) return null;
  return {
    read: recommended.read,
    modify: recommended.modify,
    approvalActions: recommended.approvalActions,
    approvalEur: recommended.approvalEur,
    only: recommended.only,
    conditions: [],
  };
}

export function AddToolPicker({
  addableNames,
  connections,
  onAdd,
  onClose,
  // Only the agent-level "Tools" tab sets these: that's the one path that
  // persists the tool the instant "Add" is clicked (agents.$id.tsx's
  // `addTool`), so a tool that takes a login (`credentialType` set) asks for
  // it here, inline, and the pick becomes the agent's fixed access for the
  // tool. The Hire dialog (new-agent-dialog.tsx) does NOT set them -- it
  // asks for the login itself in its own review step after `onAdd` just
  // marks the tool selected.
  departmentId,
  agentId,
}: {
  addableNames: string[];
  connections: McpConnection[];
  onAdd: (name: string, policy: GuardrailValue | null, connectionId?: string | null) => void;
  onClose: () => void;
  departmentId?: string | null;
  agentId?: string;
}) {
  const t = useT();
  const [search, setSearch] = useState("");
  const [useRecommendation, setUseRecommendation] = useState<Record<string, boolean>>({});
  const [pickedCredentialId, setPickedCredentialId] = useState<Record<string, string>>({});
  const connectionByName = new Map(connections.map((c) => [c.name, c] as const));
  const setAccess = useSetAgentToolAccess(agentId ?? "");

  const term = search.trim().toLowerCase();
  const filtered = term ? addableNames.filter((n) => n.toLowerCase().includes(term)) : addableNames;

  function needsLogin(name: string): boolean {
    if (departmentId === undefined) return false;
    const connection = connectionByName.get(name);
    return !!connection?.credentialType;
  }

  function pickCredential(name: string, credentialId: string) {
    setPickedCredentialId((s) => {
      const next = { ...s };
      if (credentialId) next[name] = credentialId;
      else delete next[name];
      return next;
    });
  }

  function handleAdd(name: string) {
    const connection = connectionByName.get(name);
    const recommendation = connection ? preferredPreset(connection) : null;
    const wantsRecommendation = useRecommendation[name] ?? true;
    onAdd(name, recommendation && wantsRecommendation ? recommendation : null, undefined);
    const credentialId = pickedCredentialId[name];
    if (agentId && needsLogin(name) && credentialId) {
      setAccess
        .mutateAsync({ toolKey: name, body: { mode: "fixed", credentialId } })
        .catch((err: unknown) =>
          toast.error(
            t("Could not link this credential", "Anmeldedaten konnten nicht verknüpft werden"),
            { description: err instanceof Error ? err.message : String(err) },
          ),
        );
    }
  }

  return (
    <div
      className="fixed inset-0 z-40 grid place-items-center bg-black/60 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="w-full max-w-lg overflow-hidden rounded-xl border border-border bg-panel shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-border px-5 py-4">
          <h2 className="font-serif text-xl">{t("Add tool", "Tool hinzufügen")}</h2>
          <button
            type="button"
            onClick={onClose}
            className="grid h-8 w-8 place-items-center rounded-md border border-border text-muted-foreground transition hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="relative border-b border-border px-5 py-3">
          <Search className="pointer-events-none absolute left-8 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("Search tools…", "Tools durchsuchen…")}
            className="w-full rounded-md border border-border bg-background/40 py-2 pl-8 pr-3 text-sm outline-none focus:border-primary/50"
          />
        </div>
        <div className="max-h-[60vh] divide-y divide-border overflow-y-auto">
          {filtered.length === 0 && (
            <div className="p-6 text-center text-sm text-muted-foreground">
              {t("No tools match your search.", "Keine Tools passen zur Suche.")}
            </div>
          )}
          {filtered.map((name) => {
            const connection = connectionByName.get(name);
            const recommendation = connection ? preferredPreset(connection) : null;
            const checked = useRecommendation[name] ?? true;
            const requiresLogin = needsLogin(name);
            const picked = pickedCredentialId[name] ?? "";
            return (
              <div key={name} className="px-5 py-3">
                <div className="flex items-center gap-3">
                  <Wrench className="h-4 w-4 shrink-0 text-primary" />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium">{name}</div>
                    {recommendation && (
                      <div className="mt-0.5 truncate text-xs text-muted-foreground">
                        {t("Recommended by this capa", "Empfehlung dieser Capa")}
                      </div>
                    )}
                  </div>
                  {recommendation && (
                    <label className="flex shrink-0 items-center gap-1.5 text-xs">
                      <input
                        type="checkbox"
                        aria-label={t("With recommendation", "Mit Empfehlung")}
                        checked={checked}
                        onChange={(e) =>
                          setUseRecommendation((prev) => ({ ...prev, [name]: e.target.checked }))
                        }
                      />
                      {t("With recommendation", "Mit Empfehlung")}
                    </label>
                  )}
                  <button
                    type="button"
                    onClick={() => handleAdd(name)}
                    disabled={requiresLogin && !picked}
                    className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-border bg-background/40 px-2.5 py-1.5 text-xs font-medium text-foreground transition hover:bg-background/70 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    <Plus className="h-3.5 w-3.5" /> {t("Add", "Hinzufügen")}
                  </button>
                </div>
                {requiresLogin && (
                  <div className="mt-2 pl-7">
                    <p className="mb-1 text-[11px] text-muted-foreground">
                      {t(
                        "This tool needs a login before it can be enabled.",
                        "Dieses Tool braucht einen Login, bevor es aktiviert werden kann.",
                      )}
                    </p>
                    <CredentialPicker
                      credentialType={connection!.credentialType!}
                      value={picked}
                      onChange={(credentialId) => pickCredential(name, credentialId)}
                    />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
