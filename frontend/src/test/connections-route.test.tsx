import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const { navigate } = vi.hoisted(() => ({ navigate: vi.fn() }));

vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return { ...actual, useNavigate: () => navigate };
});
vi.mock("@/components/connections/connections-page", () => ({
  ConnectionsPage: ({
    search,
    onConsumeSearch,
  }: {
    search: unknown;
    onConsumeSearch: () => void;
  }) => (
    <button type="button" onClick={onConsumeSearch}>
      {JSON.stringify(search)}
    </button>
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
    });
  });

  it("removes the pending id from the URL by replacing the history entry", () => {
    vi.spyOn(Route, "useSearch").mockReturnValue({
      pending: "p-1",
      provider: "microsoft",
    } as never);
    render(<ConnectionsRoute />);
    fireEvent.click(screen.getByRole("button"));
    expect(navigate).toHaveBeenCalledWith({ to: "/connections", search: {}, replace: true });
  });

  it("asks the browser to send no referrer", () => {
    const head = Route.options.head as unknown as () => { meta: Array<Record<string, string>> };
    expect(head().meta).toContainEqual({ name: "referrer", content: "no-referrer" });
  });
});
