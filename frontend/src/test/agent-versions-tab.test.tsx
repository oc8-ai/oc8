import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi, beforeEach } from "vitest";
import type { ReactElement } from "react";

const { getMock, postMock, toastSuccess, toastError, mayMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  postMock: vi.fn(),
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
  mayMock: vi.fn(() => true),
}));

vi.mock("@/lib/api", () => ({
  api: {
    get: (url: string) => getMock(url),
    post: (url: string, body?: unknown) => postMock(url, body),
  },
}));
vi.mock("sonner", () => ({ toast: { success: toastSuccess, error: toastError } }));
vi.mock("@/lib/governance-hooks", () => ({ useMay: () => mayMock }));

import { AgentVersionsTab } from "@/components/agent-versions-tab";

const VERSIONS = {
  items: [
    {
      id: "v3",
      versionNo: 3,
      note: "Rollback to v1",
      publishedBy: "mem-1",
      publishedAt: "2026-09-27T12:00:00+00:00",
      isCurrent: true,
      rolledBackFrom: 1,
    },
    {
      id: "v2",
      versionNo: 2,
      note: "tightened the threshold",
      publishedBy: "mem-1",
      publishedAt: "2026-09-26T12:00:00+00:00",
      isCurrent: false,
      rolledBackFrom: null,
    },
    {
      id: "v1",
      versionNo: 1,
      note: null,
      publishedBy: null,
      publishedAt: "2026-09-25T12:00:00+00:00",
      isCurrent: false,
      rolledBackFrom: null,
    },
  ],
  totalCount: 3,
};

function renderTab(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

// `api.ts`'s real `ApiError` carries only `.status` plus a `.message` that is
// `JSON.stringify(detail)` when the backend's `detail` field is an object --
// there is no `.body` property. See agent-publish-bar.test.tsx's own note.
function apiError(status: number, detail: Record<string, unknown>) {
  return Object.assign(new Error(JSON.stringify(detail)), { status });
}

describe("AgentVersionsTab", () => {
  beforeEach(() => {
    getMock.mockReset();
    postMock.mockReset();
    toastSuccess.mockReset();
    toastError.mockReset();
    mayMock.mockReset();
    mayMock.mockReturnValue(true);
    getMock.mockImplementation((url: string) => {
      if (url.includes("/versions?")) return Promise.resolve(VERSIONS);
      if (url.includes("/versions/diff")) {
        return Promise.resolve({
          fromVersionNo: 1,
          toVersionNo: 3,
          entries: [{ field: "mission", before: "old", after: "new" }],
        });
      }
      throw new Error(`unexpected GET ${url}`);
    });
  });

  it("lists versions newest first and marks the current one", async () => {
    renderTab(<AgentVersionsTab agentId="ag-1" mayManage={true} />);
    const rows = await screen.findAllByRole("row");
    // header + 3
    expect(rows).toHaveLength(4);
    expect(within(rows[1]).getByText("v3")).toBeTruthy();
    expect(within(rows[1]).getByText(/running/i)).toBeTruthy();
    expect(within(rows[3]).getByText("v1")).toBeTruthy();
  });

  it("shows that a version came from a rollback", async () => {
    renderTab(<AgentVersionsTab agentId="ag-1" mayManage={true} />);
    expect(await screen.findByText(/rolled back from v1/i)).toBeTruthy();
  });

  it("offers no Roll back on the current version", async () => {
    renderTab(<AgentVersionsTab agentId="ag-1" mayManage={true} />);
    const rows = await screen.findAllByRole("row");
    expect(within(rows[1]).queryByRole("button", { name: /roll back/i })).toBeNull();
    expect(within(rows[2]).getByRole("button", { name: /roll back/i })).toBeTruthy();
  });

  it("confirms a rollback with the diff it is about to apply", async () => {
    renderTab(<AgentVersionsTab agentId="ag-1" mayManage={true} />);
    const rows = await screen.findAllByRole("row");
    fireEvent.click(within(rows[3]).getByRole("button", { name: /roll back/i }));
    // from = the TARGET, to = the version running now: "this is what will change".
    await waitFor(() =>
      expect(getMock).toHaveBeenCalledWith("/agents/ag-1/versions/diff?from=1&to=3"),
    );
    expect(await screen.findByText("mission")).toBeTruthy();
    // Nothing posted until the confirm button is clicked.
    expect(postMock).not.toHaveBeenCalled();
  });

  it("posts the rollback only after confirmation", async () => {
    postMock.mockResolvedValue({ id: "v4", versionNo: 4, rolledBackFrom: 1 });
    renderTab(<AgentVersionsTab agentId="ag-1" mayManage={true} />);
    const rows = await screen.findAllByRole("row");
    fireEvent.click(within(rows[3]).getByRole("button", { name: /roll back/i }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /roll back to v1/i }));
    // The mocked `api.post` above always forwards its (optional) second
    // parameter positionally, so a call with no body still arrives as
    // `(url, undefined)` -- matching the exact call, not just the URL, is
    // what the real `api.post(url)` call site with no body argument produces.
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/agents/ag-1/versions/1/rollback", undefined),
    );
    await waitFor(() => expect(toastSuccess).toHaveBeenCalled());
  });

  it("explains a refused rollback with the backend's own reason", async () => {
    postMock.mockRejectedValue(
      apiError(422, { error: "publish_hook_rejected", hook: "compliance", reason: "no" }),
    );
    renderTab(<AgentVersionsTab agentId="ag-1" mayManage={true} />);
    const rows = await screen.findAllByRole("row");
    fireEvent.click(within(rows[3]).getByRole("button", { name: /roll back/i }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /roll back to v1/i }));
    await waitFor(() => expect(toastError).toHaveBeenCalled());
    expect(String(toastError.mock.calls[0]?.[1]?.description ?? "")).toMatch(/policy check/i);
  });

  it("explains a rollback refused over since-deleted dependencies", async () => {
    // Real shape from `agents/versioning.py`'s `missing_references`, as raised
    // by the rollback route in agents_write.py -- keyed by payload field, not
    // camelCased (a raw HTTPException detail dict, not a CamelModel).
    postMock.mockRejectedValue(
      apiError(422, {
        error: "version_references_missing",
        missing: { knowledge_grants: ["kb-1", "kb-2"], model_config_id: ["mc-1"] },
      }),
    );
    renderTab(<AgentVersionsTab agentId="ag-1" mayManage={true} />);
    const rows = await screen.findAllByRole("row");
    fireEvent.click(within(rows[3]).getByRole("button", { name: /roll back/i }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /roll back to v1/i }));
    await waitFor(() => expect(toastError).toHaveBeenCalled());
    const description = String(toastError.mock.calls[0]?.[1]?.description ?? "");
    expect(description).toMatch(/2 knowledge bases/i);
    expect(description).toMatch(/model configuration/i);
    expect(description).not.toMatch(/"error":"version_references_missing"/);
  });

  it("explains a rollback refused because the restored access exceeds the department frame", async () => {
    // Real shape from `authz/pdp.py`'s `SubsetViolation` dataclass, serialized
    // via `.__dict__` -- `tool_key`/`reason`, not camelCased.
    postMock.mockRejectedValue(
      apiError(422, {
        error: "narrowing_exceeds_frame",
        violations: [{ tool_key: "odoo.crm", reason: "'write' not granted by frame" }],
      }),
    );
    renderTab(<AgentVersionsTab agentId="ag-1" mayManage={true} />);
    const rows = await screen.findAllByRole("row");
    fireEvent.click(within(rows[3]).getByRole("button", { name: /roll back/i }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /roll back to v1/i }));
    await waitFor(() => expect(toastError).toHaveBeenCalled());
    const description = String(toastError.mock.calls[0]?.[1]?.description ?? "");
    expect(description).toMatch(/odoo\.crm/);
    expect(description).toMatch(/department/i);
  });

  it("offers Diff but not Roll back to a caller who may not publish", async () => {
    mayMock.mockReturnValue(false);
    renderTab(<AgentVersionsTab agentId="ag-1" mayManage={true} />);
    const rows = await screen.findAllByRole("row");
    expect(within(rows[2]).getByRole("button", { name: /diff/i })).toBeTruthy();
    expect(within(rows[2]).queryByRole("button", { name: /roll back/i })).toBeNull();
  });
});
