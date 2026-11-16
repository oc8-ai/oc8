import { render, screen } from "@testing-library/react";
import { RouterProvider } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  EditionAppGate,
  EditionCompositionProvider,
  composeFrontendEdition,
} from "@/edition/composition";
import type { FrontendEditionExtension } from "@/edition/contracts";
import { getRouter } from "../router";

// An edition may gate the app shell: its gate wraps everything outside
// `AuthGate`, so it decides whether anything of the app renders before
// `AuthGate` can redirect a visitor without a session to /login.

const gate = vi.hoisted(() => ({ mode: "pass" as "pass" | "block" }));

vi.mock("@oc8/edition-entry", async () => {
  const { createElement, Fragment } = await import("react");
  function TestGate({ children }: { children: ReactNode }) {
    return gate.mode === "block"
      ? createElement("p", null, "edition signed-out screen")
      : createElement(Fragment, null, children);
  }
  return {
    editionExtensions: [{ id: "test-gate", appGate: TestGate }],
    editionRoutes: [],
    editionServer: {},
  };
});

vi.mock("@/lib/i18n", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/i18n")>();
  return { ...actual, useT: () => (en: string) => en };
});

function stubCommunityConfig() {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ mode: "community", initialized: true }),
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function renderAt(pathname: string) {
  window.history.replaceState({}, "", pathname);
  const router = getRouter();
  await router.load();
  render(<RouterProvider router={router} />);
}

describe("an edition's app gate around AuthGate", () => {
  beforeEach(() => {
    window.localStorage.removeItem("oc8-community-token");
    window.localStorage.removeItem("oc8-dev-token");
  });

  afterEach(() => {
    gate.mode = "pass";
    vi.unstubAllGlobals();
  });

  it("shows the gate's own screen and never mounts AuthGate or the page", async () => {
    gate.mode = "block";
    const fetchMock = stubCommunityConfig();
    await renderAt("/forgot-password");

    expect(await screen.findByText("edition signed-out screen")).toBeInTheDocument();
    // AuthGate asks for /auth/config as soon as it mounts.
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(fetchMock.mock.calls.some((c) => String(c[0]).endsWith("/auth/config"))).toBe(false);
    expect(screen.queryByText("Redirecting…")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Your email")).not.toBeInTheDocument();
  });

  it("keeps AuthGate's redirect behind a gate that passes through", async () => {
    stubCommunityConfig();
    await renderAt("/welcome");

    expect(await screen.findByText("Redirecting…")).toBeInTheDocument();
  });
});

describe("composing app gates", () => {
  function Labelled(label: string) {
    return function LabelledGate({ children }: { children: ReactNode }) {
      return (
        <div data-testid={`gate-${label}`}>
          {label}
          {children}
        </div>
      );
    };
  }

  it("has none without an edition", () => {
    expect(composeFrontendEdition([]).appGates).toEqual([]);
  });

  it("renders children unchanged when no extension gates the app", () => {
    render(
      <EditionCompositionProvider extensions={[{ id: "plain" }]}>
        <EditionAppGate>
          <p>app</p>
        </EditionAppGate>
      </EditionCompositionProvider>,
    );
    expect(screen.getByText("app")).toBeInTheDocument();
  });

  it("nests several gates in extension order, the first outermost", () => {
    const extensions: FrontendEditionExtension[] = [
      { id: "a", appGate: Labelled("a") },
      { id: "b" },
      { id: "c", appGate: Labelled("c") },
    ];
    expect(composeFrontendEdition(extensions).appGates).toHaveLength(2);

    render(
      <EditionCompositionProvider extensions={extensions}>
        <EditionAppGate>
          <p>app</p>
        </EditionAppGate>
      </EditionCompositionProvider>,
    );
    const outer = screen.getByTestId("gate-a");
    const inner = screen.getByTestId("gate-c");
    expect(outer).toContainElement(inner);
    expect(inner).toContainElement(screen.getByText("app"));
  });
});
