// frontend/src/test/department-personal-admin-view.test.tsx
// A member's personal department read by an admin (`contentHidden`): its
// agents answer to the owner alone, and the API refuses anybody else's hire
// there. The page offers no "Hire agent", shows the private notice instead
// of an empty roster, and leaves out the department knowledge and feed
// panels. The owner's page is unchanged.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async (orig) => {
  const actual = await orig<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: { get, post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  };
});
vi.mock("@tanstack/react-router", async (orig) => ({
  ...(await orig<typeof import("@tanstack/react-router")>()),
  Link: ({ children }: { children: ReactNode }) => <a href="/">{children}</a>,
  useNavigate: () => vi.fn(),
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { Route } from "@/routes/departments.$id";

const DEPT_ID = "01a20cda-ca11-7000-8000-000000000001";
const NOTICE = /only that member can see and hire its agents/i;

function department(contentHidden: boolean) {
  return {
    id: DEPT_ID,
    name: "dev-member",
    icon: "user",
    goal: "",
    okr: "",
    kpiLabel: "",
    kpiValue: "",
    activity: 0,
    accent: "#000",
    promptCachingEnabled: true,
    kind: "personal",
    contentHidden,
  };
}

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

async function renderPage(contentHidden: boolean) {
  get.mockImplementation((path: string) => {
    if (path === `/departments/${DEPT_ID}`) return Promise.resolve(department(contentHidden));
    return Promise.resolve([]);
  });
  vi.spyOn(Route, "useParams").mockReturnValue({ id: DEPT_ID } as never);
  const Page = Route.options.component as () => ReactNode;
  render(<Page />, { wrapper });
  expect(await screen.findByText("Team roster")).toBeInTheDocument();
}

beforeEach(() => {
  get.mockReset();
});

describe("a member's personal department read by an admin", () => {
  it("offers no hire and shows the private notice instead of the roster and knowledge", async () => {
    await renderPage(true);
    expect(screen.queryByRole("button", { name: /hire agent/i })).toBeNull();
    expect(screen.getByText(NOTICE)).toBeInTheDocument();
    expect(screen.queryByText("Department knowledge")).toBeNull();
    expect(screen.queryByText("Live feed")).toBeNull();
  });

  it("the owner keeps the hire button, the roster and the knowledge panel", async () => {
    await renderPage(false);
    expect(screen.getByRole("button", { name: /hire agent/i })).toBeInTheDocument();
    expect(screen.queryByText(NOTICE)).toBeNull();
    expect(screen.getByText("Department knowledge")).toBeInTheDocument();
  });
});
