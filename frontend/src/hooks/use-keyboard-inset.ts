import { useEffect } from "react";

/**
 * Publishes `--keyboard-inset` on <html>: how much of the layout viewport the
 * on-screen keyboard covers. Android Chrome shrinks the layout viewport itself
 * (viewport meta `interactive-widget=resizes-content`, so `dvh` follows and
 * this stays 0); iOS Safari only shrinks the visual viewport, and a composer
 * pinned to the bottom would sit under the keyboard without this.
 * Mount once, at the root.
 */
export function useKeyboardInset(): void {
  useEffect(() => {
    const vv = window.visualViewport;
    if (!vv) return;
    const root = document.documentElement;
    const update = () => {
      const covered = Math.max(0, Math.round(window.innerHeight - vv.height - vv.offsetTop));
      root.style.setProperty("--keyboard-inset", `${covered}px`);
    };
    update();
    vv.addEventListener("resize", update);
    vv.addEventListener("scroll", update);
    return () => {
      vv.removeEventListener("resize", update);
      vv.removeEventListener("scroll", update);
      root.style.removeProperty("--keyboard-inset");
    };
  }, []);
}
