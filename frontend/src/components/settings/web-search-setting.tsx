// Settings: the tenant's web search provider (spec §10, GET/PUT
// /settings/web-search). Without one, the chat offers no "Web" switch. The
// API key is write-only: it is never shown, and saving with the field empty
// keeps the stored one.
import { useState } from "react";
import { Globe, Loader2, Save } from "lucide-react";
import { toast } from "sonner";

import { Panel } from "@/components/app-shell";
import { ApiError } from "@/lib/api";
import { useMay } from "@/lib/governance-hooks";
import { useT } from "@/lib/i18n";
import {
  useUpdateWebSearchSettings,
  useWebSearchSettings,
  type WebSearchProvider,
} from "@/lib/web-search";

const FIELD =
  "mt-1 w-full rounded-md border border-border bg-background/30 px-3 py-2 text-sm text-foreground disabled:opacity-60";

export function WebSearchSetting() {
  const t = useT();
  const may = useMay();
  const canManage = may("settings:manage");
  const { data } = useWebSearchSettings();
  const update = useUpdateWebSearchSettings();
  const [provider, setProvider] = useState<WebSearchProvider>("off");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");

  // The form starts from what the server says, and again after each save.
  const [seen, setSeen] = useState<typeof data>(undefined);
  if (data && data !== seen) {
    setSeen(data);
    setProvider(data.provider ?? "off");
    setBaseUrl(data.baseUrl ?? "");
    setApiKey("");
  }

  const keyed = provider === "brave" || provider === "tavily";
  // A stored key belongs to the tenant's own provider; another one needs its own.
  const keySaved = !!data?.hasKey && data.source === "tenant" && data.provider === provider;

  async function save() {
    const key = apiKey.trim();
    try {
      await update.mutateAsync({
        provider,
        ...(provider === "searxng" && { baseUrl: baseUrl.trim() }),
        ...(keyed && key && { apiKey: key }),
      });
      toast.success(t("Web search saved", "Websuche gespeichert"));
    } catch (error) {
      toast.error(
        !(error instanceof ApiError && error.status === 422)
          ? t("Could not save web search.", "Websuche konnte nicht gespeichert werden.")
          : provider === "searxng"
            ? t(
                "Enter the SearXNG address, starting with http:// or https://, without a login in it.",
                "Gib die SearXNG-Adresse ein, beginnend mit http:// oder https://, ohne Zugangsdaten darin.",
              )
            : t("This provider needs an API key.", "Dieser Anbieter braucht einen API-Schlüssel."),
      );
    }
  }

  return (
    <Panel id="web-search" className="p-5">
      <header className="flex items-start gap-3">
        <div className="grid h-9 w-9 place-items-center rounded-md bg-primary/15 text-primary">
          <Globe className="h-4 w-4" aria-hidden />
        </div>
        <div>
          <h3 className="font-serif text-lg">{t("Web search", "Websuche")}</h3>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {t(
              "With a provider, a person can switch on “Web” for a message and the assistant searches the web for it.",
              "Mit einem Anbieter kann man für eine Nachricht „Web“ einschalten, und der Assistent sucht dafür im Web.",
            )}
          </p>
        </div>
      </header>
      <div className="mt-4 space-y-3">
        {data?.source === "platform" && (
          <p className="text-xs text-muted-foreground">
            {t(
              "Provided by your oc8 platform. Choose a provider to override, or switch it off.",
              "Von deiner oc8-Plattform bereitgestellt. Wähle einen Anbieter, um das zu überschreiben, oder schalte es aus.",
            )}
          </p>
        )}
        <label className="block text-xs text-muted-foreground">
          {t("Provider", "Anbieter")}
          <select
            value={provider}
            disabled={!canManage || !data}
            onChange={(event) => setProvider(event.target.value as WebSearchProvider)}
            className={FIELD}
          >
            <option value="off">{t("Off", "Aus")}</option>
            <option value="searxng">SearXNG</option>
            <option value="brave">Brave</option>
            <option value="tavily">Tavily</option>
          </select>
        </label>
        {provider === "searxng" && (
          <label className="block text-xs text-muted-foreground">
            {t("SearXNG address", "SearXNG-Adresse")}
            <input
              type="url"
              value={baseUrl}
              disabled={!canManage}
              placeholder="https://search.example.org"
              onChange={(event) => setBaseUrl(event.target.value)}
              className={FIELD}
            />
          </label>
        )}
        {keyed && (
          <label className="block text-xs text-muted-foreground">
            {t("API key", "API-Schlüssel")}
            <input
              type="password"
              autoComplete="off"
              value={apiKey}
              disabled={!canManage}
              placeholder={keySaved ? t("saved", "gespeichert") : ""}
              onChange={(event) => setApiKey(event.target.value)}
              className={FIELD}
            />
          </label>
        )}
        <button
          type="button"
          onClick={() => void save()}
          disabled={!canManage || !data || update.isPending}
          className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-40"
        >
          {update.isPending ? (
            <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
          ) : (
            <Save className="h-3 w-3" aria-hidden />
          )}
          {t("Save", "Speichern")}
        </button>
      </div>
    </Panel>
  );
}
