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
    ownerActive: true,
    pendingConfirmation: false,
    ownerMemberId: "m1",
    agentCount: 2,
    followsDefaultFrame: false,
    followsDefaultBudget: true,
    createdAt: "2026-01-01T00:00:00Z",
    ...over,
  };
}

let list: unknown[];
let settingsEnabled = true;

beforeEach(() => {
  granted.current = new Set(ALL);
  list = [row()];
  settingsEnabled = true;
  getMock.mockReset();
  putMock.mockReset();
  postMock.mockReset();
  getMock.mockImplementation((path: string) => {
    if (path === "/settings/personal-departments")
      return Promise.resolve({ enabled: settingsEnabled, maxAgents: 5 });
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

  it("flags a department that waits for its owner", async () => {
    list = [row({ pendingConfirmation: true })];
    renderPanel();
    expect(await screen.findByText(/waiting for the owner/i)).toBeInTheDocument();
    expect(screen.queryByText(/no longer active/i)).not.toBeInTheDocument();
  });

  it("says the change stays held when the waiting owner has left", async () => {
    list = [row({ pendingConfirmation: true, ownerActive: false })];
    renderPanel();
    expect(await screen.findByText(/waiting for the owner/i)).toBeInTheDocument();
    expect(screen.getByText(/no longer active/i)).toBeInTheDocument();
  });

  it("shows no badge when nothing waits", async () => {
    renderPanel();
    await screen.findByText("Ada");
    expect(screen.queryByText(/waiting for the owner/i)).not.toBeInTheDocument();
  });

  it("shows a neutral placeholder for an active owner without a name, never an id", async () => {
    list = [row({ ownerName: "", ownerMemberId: "member-secret-id" })];
    renderPanel();
    expect(await screen.findByText(/member without a display name/i)).toBeInTheDocument();
    expect(screen.queryByText(/former member/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/member-secret-id/)).not.toBeInTheDocument();
  });

  it("labels an owner who has left as a former member, never an id", async () => {
    list = [row({ ownerName: "Cid", ownerActive: false, ownerMemberId: "member-secret-id" })];
    renderPanel();
    expect(await screen.findByText(/former member/i)).toBeInTheDocument();
    expect(screen.queryByText(/member without a display name/i)).not.toBeInTheDocument();
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
    const budget = screen.getByRole("region", { name: /default budget/i });
    fireEvent.click(within(budget).getByRole("button", { name: /^save$/i }));
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

  it("keeps an unsaved max-agents edit when the switch is toggled", async () => {
    putMock.mockImplementation(async () => {
      settingsEnabled = false;
      return { enabled: false, maxAgents: 5 };
    });
    renderPanel();
    const toggle = await screen.findByRole("switch", { name: /personal departments/i });
    await waitFor(() => expect(toggle).toBeEnabled());
    const max = screen.getByRole("spinbutton", { name: /max personal agents/i });
    fireEvent.change(max, { target: { value: "9" } });
    fireEvent.click(toggle);
    await waitFor(() => expect(putMock).toHaveBeenCalled());
    await waitFor(() => expect(toggle).not.toBeChecked());
    await waitFor(() => expect(toggle).toBeEnabled());
    expect(max).toHaveValue(9);
  });

  it("renders nothing without any section permission", async () => {
    granted.current = new Set();
    const { container } = renderPanel();
    expect(container).toBeEmptyDOMElement();
  });

  it("does not fetch the frame or list without department:manage", async () => {
    granted.current = new Set(["settings:view", "budget:view"]);
    renderPanel();
    await screen.findByRole("switch", { name: /personal departments/i });
    const paths = getMock.mock.calls.map((c) => c[0]);
    expect(paths).not.toContain("/personal-departments");
    expect(paths).not.toContain("/personal-departments/default-frame");
  });

  it("saves the default budget with an empty field as null", async () => {
    putMock.mockResolvedValue({ softLimitTokens: 200, hardLimitTokens: null });
    renderPanel();
    await waitFor(() =>
      expect(screen.getByRole("spinbutton", { name: /hard limit/i })).toHaveValue(1000),
    );
    fireEvent.change(screen.getByRole("spinbutton", { name: /soft limit/i }), {
      target: { value: "200" },
    });
    fireEvent.change(screen.getByRole("spinbutton", { name: /hard limit/i }), {
      target: { value: "" },
    });
    const budget = screen.getByRole("region", { name: /default budget/i });
    fireEvent.click(within(budget).getByRole("button", { name: /^save$/i }));
    await waitFor(() =>
      expect(putMock).toHaveBeenCalledWith("/personal-departments/default-budget", {
        softLimitTokens: 200,
        hardLimitTokens: null,
      }),
    );
  });

  it("rejects max-agents values outside 1..1000 client-side", async () => {
    renderPanel();
    const max = await screen.findByRole("spinbutton", { name: /max personal agents/i });
    await waitFor(() => expect(max).toHaveValue(5));
    const region = screen.getByRole("region", { name: /switch/i });
    const save = within(region).getByRole("button", { name: /^save$/i });
    for (const bad of ["0", "1001"]) {
      fireEvent.change(max, { target: { value: bad } });
      expect(save).toBeDisabled();
    }
    fireEvent.change(max, { target: { value: "6" } });
    expect(save).toBeEnabled();
  });
});
