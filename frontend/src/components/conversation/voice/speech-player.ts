// Answers read aloud (spec 2026-11-27 §5.4): one at a time across the page.
// The audio is fetched with the bearer token (an `<audio src>` cannot send
// it), buffered into a Blob URL and played; the URL is revoked when the
// reading stops or ends.
import { useCallback, useSyncExternalStore } from "react";

export type PlaybackPhase = "loading" | "playing";

interface Playback {
  key: string;
  phase: PlaybackPhase;
  controller: AbortController;
  audio: HTMLAudioElement | null;
  url: string | null;
}

let current: Playback | null = null;
const listeners = new Set<() => void>();

function changed() {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Stops the reading of `key` (any reading when left out). */
export function stopSpeech(key?: string) {
  const playback = current;
  if (!playback || (key !== undefined && playback.key !== key)) return;
  current = null;
  playback.controller.abort();
  if (playback.audio) {
    playback.audio.onended = null;
    playback.audio.pause();
  }
  if (playback.url) URL.revokeObjectURL(playback.url);
  changed();
}

/** Reads `key` aloud, stopping whatever else is being read. Rejects with
 *  what `load` or playback threw; a reading stopped meanwhile just ends. */
export async function playSpeech(
  key: string,
  load: (signal: AbortSignal) => Promise<Blob>,
): Promise<void> {
  stopSpeech();
  const playback: Playback = {
    key,
    phase: "loading",
    controller: new AbortController(),
    audio: null,
    url: null,
  };
  current = playback;
  changed();
  try {
    const blob = await load(playback.controller.signal);
    if (current !== playback) return;
    playback.url = URL.createObjectURL(blob);
    playback.audio = new Audio(playback.url);
    playback.audio.onended = () => stopSpeech(key);
    await playback.audio.play();
    if (current !== playback) return;
    playback.phase = "playing";
    changed();
  } catch (error) {
    if (current !== playback) return;
    stopSpeech(key);
    throw error;
  }
}

/** Whether `key` is being loaded or read aloud right now. */
export function useSpeechPhase(key: string): PlaybackPhase | null {
  const snapshot = useCallback(() => (current?.key === key ? current.phase : null), [key]);
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}
