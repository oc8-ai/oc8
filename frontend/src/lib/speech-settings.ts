// The tenant's speech models and daily allowances (GET/PUT /settings/speech,
// spec 2026-11-27 §9). PUT replaces the whole setting: an omitted model id is
// cleared and an omitted limit goes back to its default, so all four are sent.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";

export interface SpeechSettingsDTO {
  speechToTextModelId: string | null;
  textToSpeechModelId: string | null;
  /** Per member per day; 0 switches dictation off for members. */
  dailyTranscriptionMinutes: number;
  /** Per member per day; 0 switches reading aloud off for members. */
  dailyReadAloudCharacters: number;
}

const KEY = ["settings", "speech"] as const;

/** Fetched only while `enabled` (the caller may view settings). */
export function useSpeechSettings(enabled = true) {
  return useQuery({
    queryKey: KEY,
    enabled,
    queryFn: () => api.get<SpeechSettingsDTO>("/settings/speech"),
  });
}

export function useUpdateSpeechSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: SpeechSettingsDTO) => api.put<SpeechSettingsDTO>("/settings/speech", body),
    onSuccess: (data) => {
      qc.setQueryData(KEY, data);
      // The chat's voice buttons follow the models and allowances.
      void qc.invalidateQueries({ queryKey: ["chat", "audio", "capabilities"] });
    },
  });
}
