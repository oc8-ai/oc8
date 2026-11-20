// frontend/src/test/url-id-hardening.test.tsx
// Ids from the address bar go into API paths only when they are UUIDs, and
// every id a hook puts in a path is encoded (carried item 8).
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, renderHook, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  api: { get, post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));
vi.mock("@tanstack/react-router", async (orig) => ({
  ...(await orig<typeof import("@tanstack/react-router")>()),
  Link: ({ children }: { children: ReactNode }) => <a href="/">{children}</a>,
  useNavigate: () => vi.fn(),
}));

import { useDepartment, useKbDocuments } from "@/lib/hooks";
import { Route as DepartmentRoute } from "@/routes/departments.$id";
import { Route as KbContentRoute } from "@/routes/knowledge_.bases.$kbId.content";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const paths = () => get.mock.calls.map(([path]) => String(path));

beforeEach(() => {
  get.mockReset();
  get.mockResolvedValue([]);
});

describe("URL ids", () => {
  it("a knowledge base page with a malformed id fetches nothing and says so", async () => {
    vi.spyOn(KbContentRoute, "useParams").mockReturnValue({ kbId: "../../members" } as never);
    const Page = KbContentRoute.options.component as () => ReactNode;
    render(<Page />, { wrapper });
    expect(await screen.findByText(/This knowledge base doesn't exist/)).toBeInTheDocument();
    expect(paths().filter((p) => p.startsWith("/knowledge/bases"))).toEqual([]);
    expect(screen.queryByRole("button", { name: /Copilot/ })).toBeNull();
  });

  it("a department page with a malformed id fetches nothing and says so", async () => {
    vi.spyOn(DepartmentRoute, "useParams").mockReturnValue({ id: "../x" } as never);
    const Page = DepartmentRoute.options.component as () => ReactNode;
    render(<Page />, { wrapper });
    expect(await screen.findByText(/Department not found/)).toBeInTheDocument();
    expect(paths().filter((p) => p.startsWith("/departments"))).toEqual([]);
  });

  it("hooks encode the ids they put in a path", async () => {
    renderHook(() => useKbDocuments("a/b"), { wrapper });
    await waitFor(() => expect(paths()[0]).toMatch(/^\/knowledge\/bases\/a%2Fb\/documents/));
  });

  it("a department hook without an id asks nothing", () => {
    renderHook(() => useDepartment(""), { wrapper });
    expect(get).not.toHaveBeenCalled();
  });
});
