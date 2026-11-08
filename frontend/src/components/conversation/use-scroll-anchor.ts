// frontend/src/components/conversation/use-scroll-anchor.ts
// Spec §2.2: stick to the bottom only when the reader is already there;
// otherwise offer "↓ Latest" and leave their place alone.
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

const THRESHOLD = 48;

function scrollEl(el: HTMLElement, top: number, behavior?: ScrollBehavior) {
  // jsdom has no Element.scrollTo; a plain assignment is the same jump.
  if (typeof el.scrollTo === "function") el.scrollTo({ top, behavior });
  else el.scrollTop = top;
}

/**
 * `dep` changes whenever content grows; `resetKey` (e.g. the session id)
 * changing means a different conversation, which always opens at its end.
 */
export function useScrollAnchor(dep: unknown, resetKey?: unknown) {
  const ref = useRef<HTMLDivElement>(null);
  const [atBottom, setAtBottom] = useState(true);
  const atBottomRef = useRef(true);
  const lastReset = useRef(resetKey);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onScroll = () => {
      const near = el.scrollHeight - el.scrollTop - el.clientHeight <= THRESHOLD;
      atBottomRef.current = near;
      setAtBottom(near);
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (lastReset.current !== resetKey) {
      lastReset.current = resetKey;
      atBottomRef.current = true;
      setAtBottom(true);
    }
    if (!atBottomRef.current) return;
    scrollEl(el, el.scrollHeight);
  }, [dep, resetKey]);

  const scrollToBottom = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    scrollEl(el, el.scrollHeight, "smooth");
    atBottomRef.current = true;
    setAtBottom(true);
  }, []);

  return { ref, atBottom, scrollToBottom };
}
