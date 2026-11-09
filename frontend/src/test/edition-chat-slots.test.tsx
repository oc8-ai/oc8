// Spec §8 invariant 11.
import { render, renderHook, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

const caps = vi.hoisted(() => ({ value: null as string[] | null }));
vi.mock("@/lib/governance-hooks", async (orig) => ({
  ...(await orig<typeof import("@/lib/governance-hooks")>()),
  useCallerCapabilities: () => caps.value,
}));

import {
  EditionCompositionProvider,
  composeFrontendEdition,
  useChatSlot,
  useHasCapability,
} from "@/edition/composition";
import type { FrontendEditionExtension } from "@/edition/contracts";
import { ChatSlotProviders, MessageParts } from "@/edition/slots";

const ext: FrontendEditionExtension = {
  id: "ent",
  capabilities: ["enterprise.web_search"],
  chat: {
    messageParts: [
      {
        id: "sources",
        type: "sources",
        needs: ["enterprise.web_search"],
        component: ({ part }) => <p>sources: {String((part.items as unknown[]).length)}</p>,
      },
    ],
    composerActions: [
      { id: "mic", needs: ["enterprise.dictation"], component: () => <button>Dictate</button> },
    ],
  },
};

function withEdition(extensions: readonly FrontendEditionExtension[]) {
  return ({ children }: { children: ReactNode }) => (
    <EditionCompositionProvider extensions={extensions}>{children}</EditionCompositionProvider>
  );
}

describe("chat slots", () => {
  it("community composes every slot empty", () => {
    const { result } = renderHook(() => useChatSlot("messageParts"), {
      wrapper: withEdition([]),
    });
    expect(result.current).toEqual([]);
  });

  it("an entry without its capability renders nothing", () => {
    caps.value = ["enterprise.web_search"];
    const { result } = renderHook(() => useChatSlot("composerActions"), {
      wrapper: withEdition([ext]),
    });
    expect(result.current).toEqual([]);
    const { result: has } = renderHook(() => useHasCapability("enterprise.dictation"), {
      wrapper: withEdition([ext]),
    });
    expect(has.current).toBe(false);
  });

  it("renders a part only when its capability is held", () => {
    caps.value = ["enterprise.web_search"];
    const message = { parts: [{ type: "sources", items: [1, 2] }, { type: "unknown" }] } as never;
    const { rerender } = render(<MessageParts message={message} />, {
      wrapper: withEdition([ext]),
    });
    expect(screen.getByText("sources: 2")).toBeInTheDocument();
    caps.value = [];
    rerender(<MessageParts message={message} />);
    expect(screen.queryByText(/sources:/)).toBeNull();
  });

  it("unknown capabilities fall back to what the composition declares", () => {
    caps.value = null;
    const { result } = renderHook(() => useHasCapability("enterprise.web_search"), {
      wrapper: withEdition([ext]),
    });
    expect(result.current).toBe(true);
    const { result: none } = renderHook(() => useHasCapability("enterprise.web_search"), {
      wrapper: withEdition([]),
    });
    expect(none.current).toBe(false);
  });

  it("never hands a core part type to an extension", () => {
    caps.value = [];
    const hijack: FrontendEditionExtension = {
      id: "evil",
      chat: {
        messageParts: [
          { id: "a", type: "answers", component: () => <p>hijacked</p> },
          { id: "s", type: "suggestions", component: () => <p>hijacked</p> },
        ],
      },
    };
    const message = { parts: [{ type: "answers" }, { type: "suggestions" }] } as never;
    render(<MessageParts message={message} />, { wrapper: withEdition([hijack]) });
    expect(screen.queryByText("hijacked")).toBeNull();
  });

  it("a throwing renderer renders nothing and leaves siblings alone", () => {
    caps.value = [];
    const spy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const broken: FrontendEditionExtension = {
      id: "broken",
      chat: {
        messageParts: [
          {
            id: "boom",
            type: "boom",
            component: () => {
              throw new Error("nope");
            },
          },
          { id: "ok", type: "ok", component: () => <p>fine</p> },
        ],
      },
    };
    const message = { parts: [{ type: "boom" }, { type: "ok" }] } as never;
    render(<MessageParts message={message} />, { wrapper: withEdition([broken]) });
    expect(screen.getByText("fine")).toBeInTheDocument();
    expect(spy).toHaveBeenCalled();
    spy.mockRestore();
  });

  it("rejects a duplicate id within a slot", () => {
    const entry = { id: "x", type: "t", component: () => null };
    expect(() =>
      composeFrontendEdition([
        { id: "a", chat: { messageParts: [entry] } },
        { id: "b", chat: { messageParts: [entry] } },
      ]),
    ).toThrow(/Duplicate chat slot entry/);
  });

  it("wraps the app in global providers and survives a broken one", () => {
    caps.value = [];
    const spy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const providers: FrontendEditionExtension = {
      id: "p",
      chat: {
        globalProviders: [
          {
            id: "broken",
            component: () => {
              throw new Error("nope");
            },
          },
          {
            id: "label",
            component: ({ children }) => <section aria-label="wrapped">{children}</section>,
          },
        ],
      },
    };
    render(
      <ChatSlotProviders>
        <p>app</p>
      </ChatSlotProviders>,
      { wrapper: withEdition([providers]) },
    );
    expect(screen.getByText("app")).toBeInTheDocument();
    spy.mockRestore();
  });
});
