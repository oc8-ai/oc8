// Something that appears should stay long enough to be seen: a waiting
// state that would flash for a frame (an answer whose first word comes
// right away) is held for `minMs` once it has shown.
import { useEffect, useRef, useState } from "react";

export function useMinVisible(active: boolean, minMs: number): boolean {
  const [held, setHeld] = useState(active);
  const since = useRef(0);
  // Adjusting state while rendering (React's documented pattern): visible in
  // the same frame `active` turns on.
  if (active && !held) setHeld(true);
  useEffect(() => {
    if (held) since.current = Date.now();
  }, [held]);
  useEffect(() => {
    if (active || !held) return;
    const left = Math.max(0, minMs - (Date.now() - since.current));
    const timer = window.setTimeout(() => setHeld(false), left);
    return () => window.clearTimeout(timer);
  }, [active, held, minMs]);
  return active || held;
}
