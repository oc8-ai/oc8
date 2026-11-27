import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useKeyboardInset } from "@/hooks/use-keyboard-inset";

// iOS Safari keeps the layout viewport when the on-screen keyboard opens and
// only shrinks the visual one, so a bottom-pinned composer ends up under the
// keyboard. The hook publishes the covered height as --keyboard-inset.

function fakeViewport(height: number, offsetTop = 0) {
  const listeners = new Map<string, () => void>();
  const vv = {
    height,
    offsetTop,
    addEventListener: vi.fn((type: string, fn: () => void) => listeners.set(type, fn)),
    removeEventListener: vi.fn((type: string) => listeners.delete(type)),
  };
  Object.defineProperty(window, "visualViewport", { value: vv, configurable: true });
  return { vv, fire: (type: string) => listeners.get(type)?.(), listeners };
}

const inset = () => document.documentElement.style.getPropertyValue("--keyboard-inset");

afterEach(() => {
  Reflect.deleteProperty(window, "visualViewport");
  document.documentElement.style.removeProperty("--keyboard-inset");
});

describe("useKeyboardInset", () => {
  it("publishes how much of the layout viewport the keyboard covers", () => {
    vi.spyOn(window, "innerHeight", "get").mockReturnValue(844);
    const { vv, fire } = fakeViewport(844);
    renderHook(() => useKeyboardInset());
    expect(inset()).toBe("0px");

    vv.height = 508;
    fire("resize");
    expect(inset()).toBe("336px");

    // Safari scrolls the visual viewport up to the focused field.
    vv.offsetTop = 100;
    fire("scroll");
    expect(inset()).toBe("236px");
  });

  it("never goes negative and stops listening on unmount", () => {
    vi.spyOn(window, "innerHeight", "get").mockReturnValue(700);
    const { fire, listeners } = fakeViewport(720);
    const { unmount } = renderHook(() => useKeyboardInset());
    fire("resize");
    expect(inset()).toBe("0px");
    unmount();
    expect(listeners.size).toBe(0);
    expect(inset()).toBe("");
  });

  it("does nothing without the visual viewport API", () => {
    expect(() => renderHook(() => useKeyboardInset())).not.toThrow();
    expect(inset()).toBe("");
  });
});
