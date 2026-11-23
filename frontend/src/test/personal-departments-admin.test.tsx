import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { granted, getMock, putMock, postMock } = vi.hoisted(() => ({
  granted: { current: new Set<string>() },
  getMock: vi.fn(),
  putMock: vi.fn(),
  postMock: vi.fn(),
}));

vi.mock("@/lib/governance-hooks", () => ({
  useMay: () => (p: string) => granted.current.has(p),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { get: getMock, put: putMock, post: postMock } };
});
vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return {
    ...actual,
    Link: ({ children, params }: { children: React.ReactNode; params?: { id: string } }) => (
      <a href={`/departments/${params?.id}`}>{children}</a>
    ),
  };
});

import { ApiError } from "@/lib/api";
import { PersonalDepartmentsAdminPanel } from "@/components/personal-departments-admin";

const ALL = [
  "settings:view",
  "settings:manage",
  "department:manage",
  "budget:view",
  "budget:manage",
];

function row(over: Record<string, unknown> = {}) {
  return {
    id: "d1",
    ownerName: "Ada",
    ownerMemberId: "m1",
    agentCount: 2,
    followsDefaultFrame: false,
    followsDefaultBudget: true,
    createdAt: "2026-01-01T00:00:00Z",
    ...over,
  };
}

let list: unknown[];

beforeEach(() => {
  granted.current = new Set(ALL);
  list = [row()];
  getMock.mockReset();
  putMock.mockReset();
  postMock.mockReset();
  getMock.mockImplementation((path: string) => {
    if (path === "/settings/personal-departments")
      return Promise.resolve({ enabled: true, maxAgents: 5 });
    if (path === "/personal-departments/default-budget")
      return Promise.resolve({ softLimitTokens: null, hardLimitTokens: 1000 });
    if (path === "/personal-departments/default-frame")
      return Promise.resolve({ tools: {}, deviationCounts: {}, agentCount: 0 });
    if (path === "/personal-departments") return Promise.resolve(list);
    return Promise.resolve([]);
  });
  postMock.mockResolvedValue({});
});

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <PersonalDepartmentsAdminPanel />
    </QueryClientProvider>,
  );
}

describe("PersonalDepartmentsAdminPanel", () => {
  it("shows the switch, the default budget and the personal list with metadata only", async () => {
    renderPanel();
    const toggle = await screen.findByRole("switch", { name: /personal departments/i });
    await waitFor(() => expect(toggle).toBeChecked());
    expect(await screen.findByText("Ada")).toBeInTheDocument();
    expect(screen.getByText(/own frame/i)).toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: /max personal agents/i })).toHaveValue(5);
    expect(screen.getByRole("spinbutton", { name: /hard limit/i })).toHaveValue(1000);
    fireEvent.click(screen.getByRole("button", { name: /follow default/i }));
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/personal-departments/d1/follow-default"),
    );
  });

  it("shows a neutral placeholder for an unnamed owner, never an id", async () => {
    list = [row({ ownerName: "", ownerMemberId: "member-secret-id" })];
    renderPanel();
    expect(await screen.findByText(/former or unnamed member/i)).toBeInTheDocument();
    expect(screen.queryByText(/member-secret-id/)).not.toBeInTheDocument();
  });

  it("saves the switch with maxAgents", async () => {
    putMock.mockResolvedValue({ enabled: false, maxAgents: 5 });
    renderPanel();
    const toggle = await screen.findByRole("switch", { name: /personal departments/i });
    await waitFor(() => expect(toggle).toBeEnabled());
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(putMock).toHaveBeenCalledWith("/settings/personal-departments", {
        enabled: false,
        maxAgents: 5,
      }),
    );
  });

  it("shows a 422 from the default budget inline", async () => {
    putMock.mockRejectedValue(new ApiError("soft limit must not exceed hard limit", 422));
    renderPanel();
    await waitFor(() =>
      expect(screen.getByRole("spinbutton", { name: /hard limit/i })).toHaveValue(1000),
    );
    const soft = screen.getByRole("spinbutton", { name: /soft limit/i });
    fireEvent.change(soft, { target: { value: "5000" } });
    fireEvent.click(screen.getAllByRole("button", { name: /^save$/i })[1]);
    expect(await screen.findByText(/soft limit must not exceed hard limit/i)).toBeInTheDocument();
  });

  it("shows a 404 from follow-default inline", async () => {
    postMock.mockRejectedValue(new ApiError("not found", 404));
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: /follow default/i }));
    expect(await screen.findByText(/no longer a personal department/i)).toBeInTheDocument();
  });

  it("hides the editing controls without the permissions", async () => {
    granted.current = new Set(["settings:view", "budget:view", "department:manage"]);
    renderPanel();
    expect(await screen.findByText("Ada")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: /personal departments/i })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /follow default/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^save$/i })).not.toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: /hard limit/i })).toBeDisabled();
  });

  it("shows Follow default only with both department:manage and budget:manage", async () => {
    granted.current = new Set([...ALL].filter((p) => p !== "budget:manage"));
    renderPanel();
    expect(await screen.findByText("Ada")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /follow default/i })).not.toBeInTheDocument();
  });

  it("links the owner to the department page", async () => {
    renderPanel();
    const cell = await screen.findByText("Ada");
    expect(within(cell.closest("tr")!).getByRole("link")).toHaveAttribute(
      "href",
      "/departments/d1",
    );
  });
});
