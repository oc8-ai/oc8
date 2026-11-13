import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ContractBindingsPanel } from "@/components/contract-bindings-panel";
import { FlowsPage } from "@/routes/flows";
import { HandoffTypesPanel } from "@/routes/handoffs";

const { apiGet, apiPost, apiDelete, toastSuccess, toastError } = vi.hoisted(() => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiDelete: vi.fn(),
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      get: (url: string) => apiGet(url),
      post: (url: string, body?: unknown) => apiPost(url, body),
      delete: (url: string) => apiDelete(url),
    },
  };
});

vi.mock("sonner", async (importOriginal) => {
  const actual = await importOriginal<typeof import("sonner")>();
  return { ...actual, toast: { ...actual.toast, success: toastSuccess, error: toastError } };
});

function renderWithClient(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const departments = {
  items: [
    { id: "d-sales", name: "Vertrieb" },
    { id: "d-eng", name: "Entwicklung" },
  ],
  total: 2,
  page: 1,
  pageSize: 200,
};

function routeGet(routes: Record<string, unknown>) {
  apiGet.mockImplementation((url: string) => {
    const key = Object.keys(routes).find((k) => url.startsWith(k));
    return Promise.resolve(key ? routes[key] : []);
  });
}

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
  apiDelete.mockReset();
  toastSuccess.mockReset();
  toastError.mockReset();
});

describe("ContractBindingsPanel", () => {
  it("lists this department's bindings and removes one after confirming", async () => {
    routeGet({
      "/contract-bindings": [
        {
          id: "b1",
          eventType: "sales.deal.won",
          handoffTypeId: "ht1",
          sourceDepartmentId: "d-sales",
          targetDepartmentId: "d-eng",
        },
        {
          id: "b2",
          eventType: "other.event",
          handoffTypeId: "ht1",
          sourceDepartmentId: "d-x",
          targetDepartmentId: "d-y",
        },
      ],
      "/handoff-types": [{ id: "ht1", name: "project.kickoff", classification: "internal" }],
      "/departments": departments,
    });
    apiDelete.mockResolvedValue(undefined);
    renderWithClient(<ContractBindingsPanel departmentId="d-sales" />);

    expect(await screen.findByText("sales.deal.won")).toBeInTheDocument();
    expect(screen.queryByText("other.event")).not.toBeInTheDocument();
    expect(await screen.findByText("project.kickoff")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /remove binding sales\.deal\.won/i }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /^remove$/i }));

    await waitFor(() => expect(apiDelete).toHaveBeenCalledWith("/contract-bindings/b1"));
    await waitFor(() => expect(toastSuccess).toHaveBeenCalled());
  });

  it("does nothing when the confirmation is cancelled", async () => {
    routeGet({
      "/contract-bindings": [
        {
          id: "b1",
          eventType: "sales.deal.won",
          handoffTypeId: "ht1",
          sourceDepartmentId: "d-sales",
          targetDepartmentId: "d-eng",
        },
      ],
      "/departments": departments,
    });
    renderWithClient(<ContractBindingsPanel departmentId="d-sales" />);

    fireEvent.click(
      await screen.findByRole("button", { name: /remove binding sales\.deal\.won/i }),
    );
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /^cancel$/i }));

    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(apiDelete).not.toHaveBeenCalled();
  });
});

describe("HandoffTypesPanel", () => {
  it("shows the server's reason when a type is still in use", async () => {
    routeGet({
      "/handoff-types": [{ id: "ht1", name: "project.kickoff", classification: "internal" }],
    });
    apiDelete.mockRejectedValue(
      new Error('handoff type "project.kickoff" is still used by 2 handoffs.'),
    );
    renderWithClient(<HandoffTypesPanel />);

    fireEvent.click(await screen.findByRole("button", { name: /delete project\.kickoff/i }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /^delete$/i }));

    await waitFor(() => expect(apiDelete).toHaveBeenCalledWith("/handoff-types/ht1"));
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(expect.any(String), {
        description: expect.stringContaining("still used by 2 handoffs"),
      }),
    );
  });
});

describe("FlowsPage", () => {
  const flow = {
    id: "f1",
    name: "onboarding",
    semver: "1.0.0",
    spec: {
      trigger: { from_department_id: "d-sales" },
      stages: [{ id: "kickoff", handoff: { type: "project.kickoff", to_department_id: "d-eng" } }],
    },
    runCount: 1,
  };
  const run = {
    id: "r1",
    flowVersionId: "v1",
    flowId: "f1",
    status: "running",
    currentStages: ["kickoff"],
    context: { customer: "Acme" },
  };

  it("archives the selected flow after confirming", async () => {
    routeGet({ "/flows": [flow], "/flow-runs": [run], "/departments": departments });
    apiPost.mockResolvedValue({ id: "f1", name: "onboarding", archived: true });
    renderWithClient(<FlowsPage />);

    fireEvent.click(await screen.findByText("onboarding"));
    fireEvent.click(screen.getByRole("button", { name: /^archive$/i }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /^archive$/i }));

    await waitFor(() => expect(apiPost).toHaveBeenCalledWith("/flows/f1/archive", undefined));
  });

  it("cancels a running flow run after confirming", async () => {
    routeGet({ "/flows": [flow], "/flow-runs": [run], "/departments": departments });
    apiPost.mockResolvedValue({ ...run, status: "cancelled" });
    renderWithClient(<FlowsPage />);

    fireEvent.click(await screen.findByText("onboarding"));
    fireEvent.click(await screen.findByText("Acme"));
    fireEvent.click(screen.getByRole("button", { name: /cancel run/i }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /^cancel run$/i }));

    await waitFor(() => expect(apiPost).toHaveBeenCalledWith("/flow-runs/r1/cancel", undefined));
  });

  it("offers no cancel for a run that has ended", async () => {
    routeGet({
      "/flows": [flow],
      "/flow-runs": [{ ...run, status: "completed" }],
      "/departments": departments,
    });
    renderWithClient(<FlowsPage />);

    fireEvent.click(await screen.findByText("onboarding"));
    fireEvent.click(await screen.findByText("Acme"));
    expect(screen.queryByRole("button", { name: /cancel run/i })).not.toBeInTheDocument();
  });
});
