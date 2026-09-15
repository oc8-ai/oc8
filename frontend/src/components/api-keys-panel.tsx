// Settings -> API keys: self-service credentials authenticating the outward
// MCP gateway (`/mcp/external`, api/mcp_external.py). A key is never wider
// than its own owner's live permissions -- there is nothing to grant here
// beyond a name, where it may be called from (allowed origins), and on/off.
import { Check, Copy, KeyRound, Loader2, Plus, Power, Trash2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import {
  useApiKeys,
  useCreateApiKey,
  useDeleteApiKey,
  useUpdateApiKey,
  type ApiKeyCreatedDTO,
} from "@/lib/hooks";

export function ApiKeysPanel() {
  const t = useT();
  const { data: keys = [], isLoading } = useApiKeys();
  const createKey = useCreateApiKey();
  const updateKey = useUpdateApiKey();
  const deleteKey = useDeleteApiKey();

  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [origins, setOrigins] = useState("");
  const [revealed, setRevealed] = useState<ApiKeyCreatedDTO | null>(null);

  const handleCreate = async () => {
    const allowedOrigins = origins
      .split(",")
      .map((o) => o.trim())
      .filter(Boolean);
    try {
      const created = await createKey.mutateAsync({ name: name.trim(), allowedOrigins });
      setRevealed(created);
      setName("");
      setOrigins("");
      setCreating(false);
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : t("Could not create API key.", "API-Key konnte nicht erstellt werden."),
      );
    }
  };

  const handleToggle = async (keyId: string, enabled: boolean) => {
    try {
      await updateKey.mutateAsync({ keyId, enabled });
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : t("Could not update the key.", "Key konnte nicht aktualisiert werden."),
      );
    }
  };

  const handleDelete = async (keyId: string) => {
    try {
      await deleteKey.mutateAsync(keyId);
      toast.success(t("API key deleted", "API-Key gelöscht"));
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : t("Could not delete the key.", "Key konnte nicht gelöscht werden."),
      );
    }
  };

  return (
    <Panel id="api-keys" className="p-5 md:col-span-2">
      <header className="flex items-start gap-3">
        <div className="grid h-9 w-9 place-items-center rounded-md bg-primary/15 text-primary">
          <KeyRound className="h-4 w-4" />
        </div>
        <div className="min-w-0">
          <h3 className="font-serif text-lg">{t("API keys", "API-Keys")}</h3>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {t(
              "Lets an external tool call oc8's Copilot over MCP as you, with your own permissions.",
              "Erlaubt einem externen Tool, oc8s Copilot über MCP als Sie aufzurufen, mit Ihren eigenen Berechtigungen.",
            )}
          </p>
        </div>
        <button
          type="button"
          onClick={() => setCreating((v) => !v)}
          className="ml-auto inline-flex shrink-0 items-center gap-1.5 rounded-md border border-border bg-background/30 px-2.5 py-1.5 text-xs text-foreground hover:border-primary/50"
        >
          <Plus className="h-3.5 w-3.5" />
          {t("New key", "Neuer Key")}
        </button>
      </header>

      {creating && (
        <div className="mt-4 space-y-3 rounded-md border border-border bg-background/30 p-3">
          <label className="block text-xs text-muted-foreground">
            {t("Name", "Name")}
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t("e.g. Claude Desktop", "z. B. Claude Desktop")}
              className="mt-1 w-full rounded-md border border-border bg-background/30 px-3 py-2 text-sm text-foreground"
            />
          </label>
          <label className="block text-xs text-muted-foreground">
            {t(
              "Allowed origins (optional, comma-separated)",
              "Erlaubte Origins (optional, komma-getrennt)",
            )}
            <input
              value={origins}
              onChange={(e) => setOrigins(e.target.value)}
              placeholder="https://claude.ai"
              className="mt-1 w-full rounded-md border border-border bg-background/30 px-3 py-2 text-sm text-foreground"
            />
            <span className="mt-1 block text-[11px] text-muted-foreground">
              {t("Leave empty to allow any origin.", "Leer lassen, um jede Origin zuzulassen.")}
            </span>
          </label>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={handleCreate}
              disabled={!name.trim() || createKey.isPending}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-40"
            >
              {createKey.isPending ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <Plus className="h-3 w-3" />
              )}
              {t("Create", "Erstellen")}
            </button>
            <button
              type="button"
              onClick={() => setCreating(false)}
              className="rounded-md px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground"
            >
              {t("Cancel", "Abbrechen")}
            </button>
          </div>
        </div>
      )}

      {revealed && <RevealedToken keyDto={revealed} onDismiss={() => setRevealed(null)} />}

      {isLoading && <Loader2 className="mt-5 h-4 w-4 animate-spin text-muted-foreground" />}

      {!isLoading && keys.length === 0 && !creating && (
        <p className="mt-4 text-sm text-muted-foreground">
          {t("No API keys yet.", "Noch keine API-Keys.")}
        </p>
      )}

      {keys.length > 0 && (
        <ul className="mt-4 space-y-2">
          {keys.map((key) => (
            <li
              key={key.id}
              className="flex flex-wrap items-center gap-3 rounded-md border border-border bg-background/30 px-3 py-2 text-sm"
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate font-medium">{key.name}</span>
                  <span className="rounded-full border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                    oc8_ak_{key.tokenPrefix}…
                  </span>
                </div>
                <p className="mt-0.5 text-[11px] text-muted-foreground">
                  {key.allowedOrigins.length > 0
                    ? key.allowedOrigins.join(", ")
                    : t("Any origin", "Jede Origin")}
                  {" · "}
                  {key.lastUsedAt
                    ? t(
                        `Last used ${new Date(key.lastUsedAt).toLocaleString()}`,
                        `Zuletzt verwendet ${new Date(key.lastUsedAt).toLocaleString()}`,
                      )
                    : t("Never used", "Nie verwendet")}
                </p>
              </div>
              <button
                type="button"
                onClick={() => handleToggle(key.id, !key.enabled)}
                disabled={updateKey.isPending}
                className={cn(
                  "inline-flex shrink-0 items-center gap-1 rounded-md border px-2 py-1 text-xs disabled:cursor-not-allowed disabled:opacity-50",
                  key.enabled
                    ? "border-border text-foreground"
                    : "border-border text-muted-foreground",
                )}
              >
                <Power className="h-3 w-3" />
                {key.enabled ? t("Enabled", "Aktiviert") : t("Disabled", "Deaktiviert")}
              </button>
              <button
                type="button"
                onClick={() => handleDelete(key.id)}
                disabled={deleteKey.isPending}
                className="shrink-0 rounded-md p-1.5 text-muted-foreground hover:text-[color:var(--status-error)] disabled:cursor-not-allowed disabled:opacity-50"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function RevealedToken({ keyDto, onDismiss }: { keyDto: ApiKeyCreatedDTO; onDismiss: () => void }) {
  const t = useT();
  const [copied, setCopied] = useState(false);

  const copy = () => {
    navigator.clipboard?.writeText(keyDto.token).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  return (
    <div className="mt-4 space-y-2 rounded-md border border-primary/40 bg-primary/5 p-3">
      <p className="text-xs font-medium text-foreground">
        {t(
          "Copy this key now — it will not be shown again.",
          "Kopieren Sie diesen Key jetzt — er wird nicht erneut angezeigt.",
        )}
      </p>
      <div className="flex items-center gap-2">
        <code className="flex-1 truncate rounded-md border border-border bg-background/40 px-2 py-1.5 text-xs">
          {keyDto.token}
        </code>
        <button
          type="button"
          onClick={copy}
          className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border bg-background/40 px-2 py-1.5 text-xs hover:border-primary/50"
        >
          {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
          {copied ? t("Copied", "Kopiert") : t("Copy", "Kopieren")}
        </button>
      </div>
      <button
        type="button"
        onClick={onDismiss}
        className="text-[11px] text-muted-foreground underline decoration-dotted underline-offset-2 hover:text-foreground"
      >
        {t("I've saved it", "Ich habe ihn gespeichert")}
      </button>
    </div>
  );
}
