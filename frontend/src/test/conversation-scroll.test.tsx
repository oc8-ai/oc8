import { act, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useScrollAnchor } from "@/components/conversation/use-scroll-anchor";

function List({ items }: { items: number }) {
  const { ref, atBottom, scrollToBottom } = useScrollAnchor(items);
  return (
    <div>
      <div ref={ref} data-testid="scroller" />
      {!atBottom && <button onClick={scrollToBottom}>↓ Latest</button>}
    </div>
  );
}

function geometry(
  el: HTMLElement,
  {
    scrollTop,
    scrollHeight,
    clientHeight,
  }: { scrollTop: number; scrollHeight: number; clientHeight: number },
) {
  Object.defineProperty(el, "scrollHeight", { configurable: true, value: scrollHeight });
  Object.defineProperty(el, "clientHeight", { configurable: true, value: clientHeight });
  el.scrollTop = scrollTop;
  el.scrollTo = ((opts: ScrollToOptions) => {
    el.scrollTop = Number(opts.top);
  }) as typeof el.scrollTo;
}

function Harness() {
  const [n, setN] = useState(1);
  return (
    <>
      <List items={n} />
      <button onClick={() => setN((x) => x + 1)}>more</button>
    </>
  );
}

describe("useScrollAnchor", () => {
  it("follows new content only while the reader is at the bottom", () => {
    render(<Harness />);
    const el = screen.getByTestId("scroller");
    geometry(el, { scrollTop: 600, scrollHeight: 1000, clientHeight: 400 });
    fireEvent.scroll(el);
    geometry(el, { scrollTop: 600, scrollHeight: 1200, clientHeight: 400 });
    act(() => fireEvent.click(screen.getByText("more")));
    expect(el.scrollTop).toBe(1200);

    geometry(el, { scrollTop: 100, scrollHeight: 1200, clientHeight: 400 });
    fireEvent.scroll(el);
    expect(screen.getByText("↓ Latest")).toBeInTheDocument();
    geometry(el, { scrollTop: 100, scrollHeight: 1500, clientHeight: 400 });
    act(() => fireEvent.click(screen.getByText("more")));
    expect(el.scrollTop).toBe(100);

    fireEvent.click(screen.getByText("↓ Latest"));
    expect(el.scrollTop).toBe(1500);
  });

  it("does not flash the button while the smooth scroll travels down", () => {
    render(<Harness />);
    const el = screen.getByTestId("scroller");
    geometry(el, { scrollTop: 100, scrollHeight: 1200, clientHeight: 400 });
    fireEvent.scroll(el);
    // A smooth scroll: the click only starts the trip.
    el.scrollTo = (() => undefined) as typeof el.scrollTo;
    fireEvent.click(screen.getByText("↓ Latest"));
    el.scrollTop = 400;
    fireEvent.scroll(el);
    expect(screen.queryByText("↓ Latest")).toBeNull();
    el.scrollTop = 800;
    fireEvent.scroll(el);
    // Arrived: from here a scroll up shows it again.
    el.scrollTop = 100;
    fireEvent.scroll(el);
    expect(screen.getByText("↓ Latest")).toBeInTheDocument();
  });
});

describe("useScrollAnchor and growing content", () => {
  const observers: Array<() => void> = [];
  afterEach(() => {
    observers.length = 0;
    vi.unstubAllGlobals();
  });

  function Streaming() {
    const { ref, contentRef } = useScrollAnchor(1);
    return (
      <div ref={ref} data-testid="scroller">
        <div ref={contentRef} />
      </div>
    );
  }

  it("follows an answer that grows without a new turn, only while at the bottom", () => {
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(private cb: () => void) {
          observers.push(() => this.cb());
        }
        observe() {}
        disconnect() {}
      },
    );
    render(<Streaming />);
    const el = screen.getByTestId("scroller");
    geometry(el, { scrollTop: 600, scrollHeight: 1000, clientHeight: 400 });
    fireEvent.scroll(el);
    geometry(el, { scrollTop: 600, scrollHeight: 1300, clientHeight: 400 });
    act(() => observers.forEach((fire) => fire()));
    expect(el.scrollTop).toBe(1300);

    geometry(el, { scrollTop: 200, scrollHeight: 1300, clientHeight: 400 });
    fireEvent.scroll(el);
    geometry(el, { scrollTop: 200, scrollHeight: 1600, clientHeight: 400 });
    act(() => observers.forEach((fire) => fire()));
    expect(el.scrollTop).toBe(200);
  });
});
