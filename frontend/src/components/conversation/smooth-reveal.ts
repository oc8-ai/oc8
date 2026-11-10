// Tokens arrive in bursts -- a socket frame can carry one word or a whole
// sentence, and cache updates batch them further. The answer is kept whole,
// but shown at a steady pace: a base rate, plus a share of whatever is still
// waiting, so the words on screen never run much more than half a second
// behind what has arrived. The cut always falls between words (a word
// segmenter where the browser has one), never inside a word or a grapheme.
// When the run ends, or under reduced motion, everything shows at once.
import { useEffect, useRef, useState, useSyncExternalStore } from "react";

/** Characters per second with nothing waiting: an unhurried reading pace. */
export const BASE_RATE = 45;
/** Seconds the backlog is spread over: the rate grows with what is waiting,
 *  so the reveal stays within about this much of the stream. */
export const CATCH_UP_SECONDS = 0.5;
/** A longer gap between frames (a background tab) is counted as this. */
const MAX_FRAME_SECONDS = 0.1;
/** How long a last word may wait for its continuation before it shows. */
const SETTLE_MS = 250;
/** How far past the cursor the segmenter looks for the end of a word. */
const LOOKAHEAD = 64;

/** Characters to reveal over `seconds` with `backlog` characters waiting. */
export function revealStep(backlog: number, seconds: number): number {
  if (backlog <= 0) return 0;
  return Math.min(backlog, (BASE_RATE + backlog / CATCH_UP_SECONDS) * seconds);
}

type Segmenter = {
  segment(text: string): Iterable<{ index: number; segment: string }>;
};

let segmenter: Segmenter | null | undefined;
function wordSegmenter(): Segmenter | null {
  if (segmenter === undefined) {
    const Ctor = (Intl as unknown as { Segmenter?: new (l?: string, o?: object) => Segmenter })
      .Segmenter;
    segmenter = Ctor ? new Ctor(undefined, { granularity: "word" }) : null;
  }
  return segmenter;
}

/** The last place at or before `pos` (and after `from`) where `text` may be
 *  cut without splitting a word. The very end of a text still streaming is
 *  not one unless it ends in whitespace: the word may go on. */
export function wordBoundary(text: string, from: number, pos: number, more: boolean): number {
  const limit = Math.min(Math.floor(pos), text.length);
  if (limit <= from) return from;
  const endOk = (i: number) => i < text.length || !more || /\s$/.test(text);
  const seg = wordSegmenter();
  if (seg) {
    const windowEnd = Math.min(text.length, limit + LOOKAHEAD);
    const slice = text.slice(from, windowEnd);
    let best = from;
    for (const { index, segment } of seg.segment(slice)) {
      const end = from + index + segment.length;
      // A segment that runs to the window's end may be cut by the window.
      if (end > limit) break;
      if (end === windowEnd && windowEnd < text.length) break;
      if (endOk(end)) best = end;
    }
    return best;
  }
  // No segmenter: cut after whitespace, or right before it.
  for (let i = limit; i > from; i -= 1) {
    const before = text[i - 1];
    const after = text[i];
    if ((/\s/.test(before) || after === undefined || /\s/.test(after)) && endOk(i)) return i;
  }
  return from;
}

function subscribeReducedMotion(onChange: () => void): () => void {
  const query = window.matchMedia?.("(prefers-reduced-motion: reduce)");
  query?.addEventListener?.("change", onChange);
  return () => query?.removeEventListener?.("change", onChange);
}

export function usePrefersReducedMotion(): boolean {
  return useSyncExternalStore(
    subscribeReducedMotion,
    () => !!window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches,
    () => false,
  );
}

/**
 * The part of `text` to show now. While `pacing`, it grows towards `text`
 * frame by frame; otherwise it is `text`. A text that does not continue
 * what is shown (a new answer) starts over from what the two share; one
 * that is already there when this mounts is shown at once.
 */
export function useSmoothReveal(text: string, pacing: boolean): string {
  const [shown, setShown] = useState(() => text.length);
  const shownRef = useRef(shown);
  const cursor = useRef(shown);
  const textRef = useRef(text);
  const pacingRef = useRef(pacing);
  const known = useRef(text);
  textRef.current = text;
  pacingRef.current = pacing;

  // A text that is not an extension of the last one: keep only the shared part.
  if (!text.startsWith(known.current.slice(0, shownRef.current))) {
    let same = 0;
    const before = known.current;
    while (same < shownRef.current && before[same] === text[same]) same += 1;
    shownRef.current = same;
    cursor.current = same;
  }
  known.current = text;

  useEffect(() => {
    if (!pacing) {
      // All of it is on screen: should pacing start again (a new answer on
      // this turn), it goes on from here, not from an older cut.
      shownRef.current = textRef.current.length;
      cursor.current = shownRef.current;
      setShown(shownRef.current);
      return;
    }
    if (shownRef.current >= text.length) return;
    let frame = 0;
    let settle = 0;
    let last: number | null = null;
    const tick = (now: number) => {
      const target = textRef.current;
      const seconds = last === null ? 1 / 60 : Math.min((now - last) / 1000, MAX_FRAME_SECONDS);
      last = now;
      cursor.current = Math.max(cursor.current, shownRef.current);
      cursor.current += revealStep(target.length - cursor.current, seconds);
      const cut = wordBoundary(target, shownRef.current, cursor.current, pacingRef.current);
      if (cut > shownRef.current) {
        shownRef.current = cut;
        setShown(cut);
      }
      // Stop at the end of what arrived; the next fragment starts it again.
      if (cursor.current < target.length) {
        frame = requestAnimationFrame(tick);
      } else if (shownRef.current < target.length) {
        // The last word may go on -- unless nothing follows for a moment (a
        // tool call, the end of a sentence): then it is a word, show it.
        settle = window.setTimeout(() => {
          shownRef.current = target.length;
          setShown(target.length);
        }, SETTLE_MS);
      }
    };
    frame = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(frame);
      window.clearTimeout(settle);
    };
  }, [text, pacing]);

  if (!pacing) return text;
  return text.slice(0, Math.min(shownRef.current, shown, text.length));
}
