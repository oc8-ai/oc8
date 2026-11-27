// The tenant's "Run scripts in previews" setting (GET/PUT
// /settings/previews). Off: every artifact preview in the chat is static.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";

export interface PreviewSettingsDTO {
  runScripts: boolean;
}

const KEY = ["settings", "previews"] as const;

/** Fetched only while `enabled` (the chat asks once a preview opens). */
export function usePreviewSettings(enabled = true) {
  return useQuery({
    queryKey: KEY,
    enabled,
    queryFn: () => api.get<PreviewSettingsDTO>("/settings/previews"),
    staleTime: 60_000,
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
