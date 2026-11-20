import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  ComposerHint,
  SigilPopover,
  activeSigil,
  cycleIndex,
  replaceSigil,
  type SigilItem,
} from "@/components/composer-sigils";

describe("finding the sigil under the caret", () => {
  it("finds a command at the very start", () => {
    expect(activeSigil("/pl", 3)).toEqual({ kind: "/", query: "pl", start: 0, end: 3 });
  });

  it("does not treat a slash anywhere else as a command", () => {
    // Dates, paths and "and/or" have to be typeable.
    expect(activeSigil("due 12/05", 9)).toBeNull();
    expect(activeSigil("read /etc/passwd", 16)).toBeNull();
  });

  it("finds an agent mention mid-sentence", () => {
    expect(activeSigil("ask @sal", 8)).toEqual({ kind: "@", query: "sal", start: 4, end: 8 });
  });

  it("finds a context reference mid-sentence", () => {
    expect(activeSigil("check #pre", 10)).toEqual({ kind: "#", query: "pre", start: 6, end: 10 });
  });

  it("needs whitespace before the sigil", () => {
    // "a@b.com" is an email address, not a mention.
    expect(activeSigil("mail a@b", 8)).toBeNull();
  });

  it("stops at whitespace, so a finished token is no longer active", () => {
    expect(activeSigil("@sales expert", 13)).toBeNull();
  });

  it("finds nothing in plain text", () => {
    expect(activeSigil("how many tickets are open?", 26)).toBeNull();
  });

  it("finds a bare sigil with no query yet", () => {
    expect(activeSigil("ask @", 5)).toEqual({ kind: "@", query: "", start: 4, end: 5 });
  });

  it("uses the caret, not the end of the text", () => {
    expect(activeSigil("@sa and more", 3)).toEqual({ kind: "@", query: "sa", start: 0, end: 3 });
  });
});

describe("replacing the sigil once something is picked", () => {
  it("swaps the token and keeps the rest", () => {
    const token = activeSigil("ask @sal", 8)!;
    expect(replaceSigil("ask @sal", token, "")).toBe("ask ");
  });

  it("can substitute text in place", () => {
    const token = activeSigil("/pl", 3)!;
    expect(replaceSigil("/pl", token, "/plan ")).toBe("/plan ");
  });
});

describe("keyboard cycling", () => {
  it("wraps forwards and backwards", () => {
    expect(cycleIndex(2, 1, 3)).toBe(0);
    expect(cycleIndex(0, -1, 3)).toBe(2);
    expect(cycleIndex(0, 1, 0)).toBe(0);
  });
});

const ITEMS: SigilItem[] = [
  { id: "a", label: "ask", hint: "Answer from what you already know." },
  { id: "b", label: "plan", hint: "Work out the steps." },
  { id: "c", label: "finance", hint: "Finn", blocked: "You may not start runs for this agent." },
];

describe("the popover", () => {
  it("lists every item with its one-line description", () => {
    render(
      <SigilPopover
        title="Commands"
        items={ITEMS}
        activeIndex={0}
        onPick={() => {}}
        onHoverIndex={() => {}}
        emptyText="nothing"
      />,
    );
    expect(screen.getByText("ask")).toBeInTheDocument();
    expect(screen.getByText("Answer from what you already know.")).toBeInTheDocument();
  });

  it("picks an item on click", () => {
    const onPick = vi.fn();
    render(
      <SigilPopover
        title="Commands"
        items={ITEMS}
        activeIndex={0}
        onPick={onPick}
        onHoverIndex={() => {}}
        emptyText="nothing"
      />,
    );
    fireEvent.click(screen.getByRole("option", { name: /plan/ }));
    expect(onPick).toHaveBeenCalledWith(ITEMS[1]);
  });

  it("shows a blocked item's reason and refuses to pick it", () => {
    const onPick = vi.fn();
    render(
      <SigilPopover
        title="Agents"
        items={ITEMS}
        activeIndex={0}
        onPick={onPick}
        onHoverIndex={() => {}}
        emptyText="nothing"
      />,
    );
    expect(screen.getByText("You may not start runs for this agent.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("option", { name: /finance/ }));
    expect(onPick).not.toHaveBeenCalled();
  });

  it("says so when there is nothing to offer", () => {
    render(
      <SigilPopover
        title="Commands"
        items={[]}
        activeIndex={0}
        onPick={() => {}}
        onHoverIndex={() => {}}
        emptyText="No command matches that."
      />,
    );
    expect(screen.getByText("No command matches that.")).toBeInTheDocument();
  });

  it("marks the active row for a screen reader", () => {
    render(
      <SigilPopover
        title="Commands"
        items={ITEMS}
        activeIndex={1}
        onPick={() => {}}
        onHoverIndex={() => {}}
        emptyText="nothing"
      />,
    );
    expect(screen.getByRole("option", { name: /plan/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("option", { name: /ask/ })).toHaveAttribute("aria-selected", "false");
  });
});

describe("the hint", () => {
  it("names all three sigils, always visible", () => {
    // §5.2: the affordance must be discoverable, not tribal knowledge.
    render(<ComposerHint />);
    const hint = screen.getByTestId("composer-hint");
    expect(hint.textContent).toContain("/");
    expect(hint.textContent).toContain("@");
    expect(hint.textContent).toContain("#");
  });
});
