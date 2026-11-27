// Web search (spec §10): whether the composer offers its "Web" switch
// (GET /chat/web-search), the person's last choice for it (kept in this
// browser), and the tenant's provider setting (GET/PUT /settings/web-search).
import { useCallback, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";

export type WebSearchProvider = "searxng" | "brave" | "tavily" | "off";

export interface WebSearchSettingsDTO {
  /** The provider in effect; null when there is none at all. */
  provider: WebSearchProvider | null;
  /** The tenant's own SearXNG address; never the platform's. */
  baseUrl: string | null;
  /** Whether the tenant stored an API key. The key itself never comes back. */
  hasKey: boolean;
  source: "tenant" | "platform" | null;
  /** Whether the platform has a default to fall back to; nothing else about it. */
  platformAvailable: boolean;
}

export interface WebSearchSettingsUpdate {
  provider: WebSearchProvider | null;
  baseUrl?: string | null;
  /** Write-only. Left out, the stored key is kept. */
  apiKey?: string;
}

const SETTINGS_KEY = ["settings", "web-search"] as const;
const AVAILABLE_KEY = ["chat", "web-search"] as const;
export const WEB_CHOICE_STORAGE_KEY = "oc8-chat-web-search";

/** Whether this tenant (or the platform) has a search provider. */
export function useWebSearchAvailable(): boolean {
  const { data } = useQuery({
    queryKey: AVAILABLE_KEY,
    queryFn: () => api.get<{ available: boolean }>("/chat/web-search"),
    staleTime: 60_000,
  });
  return data?.available === true;
}

function readChoice(): boolean {
  try {
    return window.localStorage.getItem(WEB_CHOICE_STORAGE_KEY) === "1";
  } catch {
    return false; // private window, blocked storage: off, as by default
  }
}

/** The composer's "Web" switch: off by default, and the person's last
 *  choice in this browser after that. */
export function useWebChoice(): [boolean, (on: boolean) => void] {
  const [on, setOn] = useState(readChoice);
  const set = useCallback((next: boolean) => {
    setOn(next);
    try {
      window.localStorage.setItem(WEB_CHOICE_STORAGE_KEY, next ? "1" : "0");
    } catch {
      /* the choice still holds for this visit */
    }
  }, []);
  return [on, set];
}

export function useWebSearchSettings() {
  return useQuery({
    queryKey: SETTINGS_KEY,
    queryFn: () => api.get<WebSearchSettingsDTO>("/settings/web-search"),
  });
}

export function useUpdateWebSearchSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: WebSearchSettingsUpdate) =>
      api.put<WebSearchSettingsDTO>("/settings/web-search", body),
    onSuccess: (data) => {
      qc.setQueryData(SETTINGS_KEY, data);
      // The composer's switch follows the provider.
      void qc.invalidateQueries({ queryKey: AVAILABLE_KEY });
    },
  });
}
