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

function nearBottom(el: HTMLElement): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= THRESHOLD;
}

/**
 * `ref` goes on the scroller, `contentRef` on the column inside it: content
 * that grows without a new turn (a streaming answer, an image loading) is
 * followed through a ResizeObserver. `dep` changes whenever turns are added;
 * `resetKey` (the session id) changing means a different conversation, which
 * always opens at its end.
 */
export function useScrollAnchor(dep: unknown, resetKey?: unknown) {
  const ref = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const [atBottom, setAtBottom] = useState(true);
  const atBottomRef = useRef(true);
  // True while a "↓ Latest" smooth scroll is on its way down: the scroll
  // events it fires on the way are not the reader leaving the bottom.
  const pinning = useRef(false);
  const lastReset = useRef(resetKey);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onScroll = () => {
      const near = nearBottom(el);
      if (pinning.current) {
        if (near) pinning.current = false;
        return;
      }
      atBottomRef.current = near;
      setAtBottom(near);
    };
    // The reader taking the wheel cancels the trip down.
    const onUserScroll = () => {
      pinning.current = false;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    el.addEventListener("wheel", onUserScroll, { passive: true });
    el.addEventListener("touchstart", onUserScroll, { passive: true });
    let observer: ResizeObserver | undefined;
    const content = contentRef.current;
    if (content && typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(() => {
        if (atBottomRef.current) scrollEl(el, el.scrollHeight);
      });
      observer.observe(content);
    }
    return () => {
      el.removeEventListener("scroll", onScroll);
      el.removeEventListener("wheel", onUserScroll);
      el.removeEventListener("touchstart", onUserScroll);
      observer?.disconnect();
    };
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
    atBottomRef.current = true;
    pinning.current = true;
    setAtBottom(true);
    scrollEl(el, el.scrollHeight, "smooth");
  }, []);

  return { ref, contentRef, atBottom, scrollToBottom };
}
