import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const { navigate } = vi.hoisted(() => ({ navigate: vi.fn() }));

vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return { ...actual, useNavigate: () => navigate };
});
vi.mock("@/components/connections/connections-page", () => ({
  CONNECTIONS_TABS: ["mine", "organisation", "members"],
  ConnectionsPage: ({
    search,
    onConsumeSearch,
    onTabChange,
  }: {
    search: unknown;
    onConsumeSearch: () => void;
    onTabChange: (tab: string) => void;
  }) => (
    <>
      <button type="button" onClick={onConsumeSearch}>
        {JSON.stringify(search)}
      </button>
      <button type="button" onClick={() => onTabChange("members")}>
        members
      </button>
      <button type="button" onClick={() => onTabChange("mine")}>
        mine
      </button>
    </>
  ),
}));

import { ConnectionsRoute, Route } from "@/routes/connections";

describe("/connections route", () => {
  it("reads the OAuth return's pending id from the query", () => {
    const validate = Route.options.validateSearch as (s: Record<string, unknown>) => unknown;
    expect(validate({ pending: "p-1", provider: "microsoft", other: "x", error: "" })).toEqual({
      pending: "p-1",
      error: undefined,
      provider: "microsoft",
      connect: undefined,
      tab: undefined,
    });
  });

  it("reads a known tab from the query and drops anything else", () => {
    const validate = Route.options.validateSearch as (s: Record<string, unknown>) => {
      tab?: string;
    };
    expect(validate({ tab: "organisation" }).tab).toBe("organisation");
    expect(validate({ tab: "members" }).tab).toBe("members");
    expect(validate({ tab: "mine" }).tab).toBeUndefined();
    expect(validate({ tab: "evil" }).tab).toBeUndefined();
  });

  it("writes the chosen tab into the URL, the default tab as a clean URL", () => {
    navigate.mockReset();
    vi.spyOn(Route, "useSearch").mockReturnValue({} as never);
    render(<ConnectionsRoute />);
    fireEvent.click(screen.getByRole("button", { name: "members" }));
    expect(navigate).toHaveBeenCalledWith({
      to: "/connections",
      search: { tab: "members" },
      replace: true,
    });
    fireEvent.click(screen.getByRole("button", { name: "mine" }));
    expect(navigate).toHaveBeenLastCalledWith({ to: "/connections", search: {}, replace: true });
  });

  it("keeps the open tab when it cleans the return values out of the URL", () => {
    navigate.mockReset();
    vi.spyOn(Route, "useSearch").mockReturnValue({
      pending: "p-1",
      provider: "microsoft",
      tab: "organisation",
    } as never);
    render(<ConnectionsRoute />);
    fireEvent.click(screen.getByRole("button", { name: /pending/ }));
    expect(navigate).toHaveBeenCalledWith({
      to: "/connections",
      search: { tab: "organisation" },
      replace: true,
    });
  });

  it("removes the pending id from the URL by replacing the history entry", () => {
    vi.spyOn(Route, "useSearch").mockReturnValue({
      pending: "p-1",
      provider: "microsoft",
    } as never);
    render(<ConnectionsRoute />);
    fireEvent.click(screen.getByRole("button", { name: /pending/ }));
    expect(navigate).toHaveBeenCalledWith({ to: "/connections", search: {}, replace: true });
  });

  it("asks the browser to send no referrer", () => {
    const head = Route.options.head as unknown as () => { meta: Array<Record<string, string>> };
    expect(head().meta).toContainEqual({ name: "referrer", content: "no-referrer" });
  });
});
