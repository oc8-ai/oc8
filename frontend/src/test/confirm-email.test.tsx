import { fireEvent, render, screen } from "@testing-library/react";
import { RouterProvider } from "@tanstack/react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { getRouter } from "../router";

// `Route.useSearch()` reads real router state, so these render through the
// full router (mirrors `reset-password.test.tsx` and `login.test.tsx`'s
// "outside the application shell" tests) rather than mounting the bare
// component.
const { navigate } = vi.hoisted(() => ({ navigate: vi.fn() }));

vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return { ...actual, useNavigate: () => navigate };
});

vi.mock("@/lib/i18n", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/i18n")>();
  return { ...actual, useT: () => (en: string) => en };
});

async function renderAt(pathname: string) {
  window.history.replaceState({}, "", pathname);
  const router = getRouter();
  await router.load();
  render(<RouterProvider router={router} />);
}

/** `__root.tsx`'s `AuthGate` fetches `/auth/config` on every route, including
 *  this one -- it has to answer "community" (and PUBLIC_ROUTES has to list
 *  `/confirm-email`, which it does) or a session-less visit here bounces to
 *  DevSignIn/`/login` before the route under test ever renders. Anything
 *  else routes to `endpointResponse`, the one this test actually cares about. */
function stubFetch(endpointResponse: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/auth/config")) {
        return { ok: true, json: async () => ({ mode: "community", initialized: true }) };
      }
      return endpointResponse;
    }),
  );
}

describe("Confirm-email route", () => {
  beforeEach(() => {
    navigate.mockReset();
  });

  it("posts the token from the query string with no bearer header, then shows success", async () => {
    stubFetch({ status: 200, ok: true, json: async () => ({ id: "m1" }) });
    const fetchMock = vi.mocked(fetch);

    await renderAt("/confirm-email?token=Gx3kP0tq_fM2-aN8vR5yZ1cW7eH4jL9uB6dS0oQiT2k");

    expect(await screen.findByText("Your email has been updated.")).toBeInTheDocument();

    const confirmCall = fetchMock.mock.calls.find(([u]) =>
      String(u).includes("/auth/email/confirm"),
    );
    expect(confirmCall).toBeDefined();
    const [url, init] = confirmCall!;
    expect(String(url)).toContain("/auth/email/confirm");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({
      token: "Gx3kP0tq_fM2-aN8vR5yZ1cW7eH4jL9uB6dS0oQiT2k",
    });
    // No Authorization header: this endpoint is `unguarded` on the backend --
    // the mailed link is the credential, not a bearer session.
    expect((init?.headers as Record<string, string> | undefined)?.authorization).toBeUndefined();

    fireEvent.click(screen.getByRole("button", { name: "Back to your profile" }));
    expect(navigate).toHaveBeenCalledWith({ to: "/profile" });
  });

  it("shows the generic invalid-link message and never calls the confirm endpoint when there is no token at all", async () => {
    stubFetch({ status: 200, ok: true, json: async () => ({ id: "m1" }) });
    const fetchMock = vi.mocked(fetch);

    await renderAt("/confirm-email");

    expect(await screen.findByText("This link is invalid or has expired.")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes("/auth/email/confirm"))).toBe(
      false,
    );
  });

  it("shows the generic invalid-link message when the backend rejects the token, never a specific reason", async () => {
    stubFetch({ status: 400, ok: false, json: async () => ({ detail: "token expired" }) });

    await renderAt("/confirm-email?token=Stale0tq_fM2-aN8vR5yZ1cW7eH4jL9uB6dS0oQiT2k");

    expect(await screen.findByText("This link is invalid or has expired.")).toBeInTheDocument();
    expect(screen.queryByText(/token expired/i)).not.toBeInTheDocument();
  });

  it("takes the token out of the address bar once it has read it", async () => {
    stubFetch({ status: 200, ok: true, json: async () => ({ id: "m1" }) });
    await renderAt("/confirm-email?token=Nw3kP0tq_fM2-aN8vR5yZ1cW7eH4jL9uB6dS0oQiT2k");
    expect(await screen.findByText("Your email has been updated.")).toBeInTheDocument();
    expect(window.location.search).toBe("");
    expect(window.location.href).not.toContain("Nw3kP0tq_fM2-aN8vR5yZ1cW7eH4jL9uB6dS0oQiT2k");
  });

  it("spends the token once, even when the page mounts twice", async () => {
    stubFetch({ status: 200, ok: true, json: async () => ({ id: "m1" }) });
    const fetchMock = vi.mocked(fetch);
    await renderAt("/confirm-email?token=Tw1cE0tq_fM2-aN8vR5yZ1cW7eH4jL9uB6dS0oQiT2k");
    expect(await screen.findByText("Your email has been updated.")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.filter(([u]) => String(u).includes("/auth/email/confirm")),
    ).toHaveLength(1);
  });

  it("never posts a malformed token", async () => {
    stubFetch({ status: 200, ok: true, json: async () => ({ id: "m1" }) });
    const fetchMock = vi.mocked(fetch);
    await renderAt("/confirm-email?token=../../members%2Fx");
    expect(await screen.findByText("This link is invalid or has expired.")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes("/auth/email/confirm"))).toBe(
      false,
    );
  });
});
