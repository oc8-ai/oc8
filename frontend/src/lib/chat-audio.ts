// Voice in the chat (spec 2026-11-27 §5.4, §9): which voice buttons the chat
// shows (GET /chat/audio/capabilities). All false when the tenant has no
// speech model or the member's daily allowance is 0. The two speech calls
// themselves are `transcribeChatAudio` / `fetchAnswerSpeech` in lib/api.ts.
import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";

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
