// What a model config is for (spec 2026-11-27 §9). A speech model (dictation,
// reading aloud) is never a chat or agent model: every picker that assigns a
// model to a chat, an agent or the auto router lists `chatModels(...)`.
// Its own module so tests that mock `@/lib/hooks` wholesale need no stub.
import type { ModelDTO } from "@/lib/hooks";

export function isSpeechModel(m: Pick<ModelDTO, "capability">): boolean {
  return m.capability === "speech_to_text" || m.capability === "text_to_speech";
}

/** The models a chat, an agent or the auto router may run on. */
export function chatModels<T extends Pick<ModelDTO, "capability">>(models: T[]): T[] {
  return models.filter((m) => !isSpeechModel(m));
}
