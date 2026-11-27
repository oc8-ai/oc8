// Answers read aloud (spec 2026-11-27 §5.4): one at a time across the page.
// The audio is fetched with the bearer token (an `<audio src>` cannot send
// it), buffered into a Blob URL and played; the URL is revoked when the
// reading stops or ends.
//
// Safari and iOS only let an element play that was started inside the tap:
// `playSpeech` must be called from the click handler. It makes the element
// there and starts a silent clip on it, and plays the answer on that same
// element once fetched. If the browser still refuses, the fetched answer is
// kept, so the next tap plays it without a second (billed) request.
import { useCallback, useSyncExternalStore } from "react";

export type PlaybackPhase = "loading" | "playing";

/** A short silent WAV, played inside the tap to unlock the element. */
const SILENT =
  "data:audio/wav;base64,UklGRiYAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQIAAAAAAA==";

/** The browser refused to start playback outside a tap; the next tap plays. */
export class PlaybackBlocked extends Error {}

interface Playback {
  key: string;
  phase: PlaybackPhase;
  controller: AbortController;
  audio: HTMLAudioElement;
  url: string | null;
}

let current: Playback | null = null;
/** A reading that was fetched but not allowed to start. */
let parked: { key: string; url: string } | null = null;
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
  if (parked && (key === undefined || parked.key === key)) {
    URL.revokeObjectURL(parked.url);
    parked = null;
  }
  const playback = current;
  if (!playback || (key !== undefined && playback.key !== key)) return;
  current = null;
  playback.controller.abort();
  playback.audio.onended = null;
  playback.audio.onerror = null;
  playback.audio.pause();
  if (playback.url) URL.revokeObjectURL(playback.url);
  changed();
}

function isNotAllowed(error: unknown): boolean {
  return (error as { name?: string } | null)?.name === "NotAllowedError";
}

/** Reads `key` aloud, stopping whatever else is being read. Call it from the
 *  click itself. Rejects with `PlaybackBlocked`, or with what `load` or
 *  playback threw; a reading stopped meanwhile just ends. */
export function playSpeech(
  key: string,
  load: (signal: AbortSignal) => Promise<Blob>,
): Promise<void> {
  const reuse = parked?.key === key ? parked.url : null;
  if (reuse) parked = null;
  stopSpeech();
  const audio = new Audio();
  if (!reuse) {
    audio.src = SILENT;
    void audio.play()?.catch(() => undefined);
  }
  const playback: Playback = {
    key,
    phase: "loading",
    controller: new AbortController(),
    audio,
    url: reuse,
  };
  current = playback;
  changed();

  // Runs synchronously up to the first await: a kept answer starts in the tap.
  // `failed`: the element could not play what was fetched. Its `error` stops
  // the reading before `play()` rejects, which must not pass for a Stop.
  let failed = false;
  return (async () => {
    try {
      const url = reuse ?? URL.createObjectURL(await load(playback.controller.signal));
      if (current !== playback) {
        URL.revokeObjectURL(url);
        return;
      }
      playback.url = url;
      audio.onended = () => stopSpeech(key);
      audio.onerror = () => {
        failed = true;
        stopSpeech(key);
      };
      audio.src = url;
      try {
        await audio.play();
      } catch (error) {
        if (current !== playback || !isNotAllowed(error)) throw error;
        // Keep what was fetched for the next tap.
        current = null;
        playback.audio.onended = null;
        playback.audio.onerror = null;
        parked = { key, url };
        changed();
        throw new PlaybackBlocked();
      }
      if (failed) throw new Error("The audio could not be played.");
      if (current !== playback) return;
      playback.phase = "playing";
      changed();
    } catch (error) {
      if (error instanceof PlaybackBlocked) throw error;
      if (current !== playback && !failed) return;
      if (current === playback) stopSpeech(key);
      throw error;
    }
  })();
}

/** Whether `key` is being loaded or read aloud right now. */
export function useSpeechPhase(key: string): PlaybackPhase | null {
  const snapshot = useCallback(() => (current?.key === key ? current.phase : null), [key]);
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}
