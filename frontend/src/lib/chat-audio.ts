// Voice in the chat (spec 2026-11-27 §5.4, §9): which voice buttons the chat
// shows (GET /chat/audio/capabilities). All false when the tenant has no
// speech model or the member's daily allowance is 0. The two speech calls
// themselves are `transcribeChatAudio` / `fetchAnswerSpeech` in lib/api.ts.
import { useQuery } from "@tanstack/react-query";

import { ApiError, api } from "@/lib/api";

export interface ChatAudioCapabilitiesDTO {
  speechToText: boolean;
  textToSpeech: boolean;
  /** The read-aloud model is local, so a restricted answer may be read. */
  textToSpeechLocal: boolean;
}

const NONE: ChatAudioCapabilitiesDTO = {
  speechToText: false,
  textToSpeech: false,
  textToSpeechLocal: false,
};

/** Until it is known (or when it cannot be fetched), no voice button shows. */
export function useChatAudioCapabilities(enabled = true): ChatAudioCapabilitiesDTO {
  const { data } = useQuery({
    queryKey: ["chat", "audio", "capabilities"] as const,
    queryFn: () => api.get<ChatAudioCapabilitiesDTO>("/chat/audio/capabilities"),
    enabled,
    staleTime: 60_000,
  });
  return data ?? NONE;
}

/** The server's cap on one recording (413 above it); checked before upload. */
export const MAX_DICTATION_BYTES = 12 * 1024 * 1024;

/** The server's own reason, for the refusals where it is the useful one
 *  (too long, too large, allowance used up); null for anything else. */
export function speechRefusal(error: unknown): string | null {
  return error instanceof ApiError && [409, 413, 429].includes(error.status) ? error.message : null;
}
