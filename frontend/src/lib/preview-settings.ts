// The tenant's "Run scripts in previews" setting (GET/PUT
// /settings/previews). Off: every artifact preview in the chat is static.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";

export interface PreviewSettingsDTO {
  runScripts: boolean;
}

const KEY = ["settings", "previews"] as const;

/** Fetched only while `enabled` (the chat asks once a preview opens), and
 *  then every minute: an admin turning scripts off reaches previews that are
 *  already open within a minute. */
export function usePreviewSettings(enabled = true) {
  return useQuery({
    queryKey: KEY,
    enabled,
    queryFn: () => api.get<PreviewSettingsDTO>("/settings/previews"),
    staleTime: 60_000,
    refetchInterval: enabled ? 60_000 : false,
  });
}

export function useUpdatePreviewSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: PreviewSettingsDTO) =>
      api.put<PreviewSettingsDTO>("/settings/previews", body),
    onSuccess: (data) => qc.setQueryData(KEY, data),
  });
}
