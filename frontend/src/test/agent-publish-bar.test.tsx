import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
vi.mock("sonner", () => ({
  toast: { success: toastSuccess, error: toastError },
}));
vi.mock("@/lib/governance-hooks", () => ({
  useMay: () => mayMock,
}));

import { AgentPublishBar } from "@/components/agent-publish-bar";

function renderBar(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

// `api.ts`'s real `ApiError` carries only `.status` plus a `.message` that is
// `JSON.stringify(detail)` when the backend's `detail` field is an object
// (see `toError` in lib/api.ts) -- there is no `.body` property. These two
// refusals are built the same way the real client would throw them.
function apiError(status: number, detail: Record<string, unknown>) {
  return Object.assign(new Error(JSON.stringify(detail)), { status });
}

describe("AgentPublishBar", () => {
  beforeEach(() => {
    getMock.mockReset();
    postMock.mockReset();
    toastSuccess.mockReset();
    toastError.mockReset();
    mayMock.mockReset();
    mayMock.mockReturnValue(true);
  });

  it("renders nothing when the working copy is clean", async () => {
    getMock.mockResolvedValue({ dirty: false, changedFields: [], currentVersionNo: 3 });
    const { container } = renderBar(<AgentPublishBar agentId="ag-1" mayManage={true} />);
    await waitFor(() => expect(getMock).toHaveBeenCalled());
    expect(container.textContent).toBe("");
  });

  it("counts the changed fields", async () => {
    getMock.mockResolvedValue({
      dirty: true,
      changedFields: ["mission", "narrowing.odoo"],
      currentVersionNo: 3,
    });
    renderBar(<AgentPublishBar agentId="ag-1" mayManage={true} />);
    expect(await screen.findByText(/2 unpublished changes/i)).toBeTruthy();
  });

  it("says one change in the singular", async () => {
    getMock.mockResolvedValue({
      dirty: true,
      changedFields: ["mission"],
      currentVersionNo: 3,
    });
    renderBar(<AgentPublishBar agentId="ag-1" mayManage={true} />);
    // Exact text, not a regex probing for "not followed by s": the count and
    // the "running: vN" note are separate DOM nodes (the latter is its own
    // <span>), so @testing-library's default text matcher -- which only reads
    // an element's OWN text nodes, not descendants' -- sees "1 unpublished
    // change" on its own with nothing trailing it to test a lookahead against.
    expect(await screen.findByText("1 unpublished change")).toBeTruthy();
  });

  it("sends the current version number as the expected one when publishing", async () => {
    getMock.mockResolvedValue({
      dirty: true,
      changedFields: ["mission"],
      currentVersionNo: 3,
    });
    postMock.mockResolvedValue({ id: "v4", versionNo: 4 });
    renderBar(<AgentPublishBar agentId="ag-1" mayManage={true} />);
    fireEvent.click(await screen.findByRole("button", { name: /publish/i }));
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/agents/ag-1/versions", {
        note: undefined,
        expectedCurrentVersionNo: 3,
      }),
    );
    await waitFor(() => expect(toastSuccess).toHaveBeenCalled());
  });

  it("surfaces a 409 as a refetch instruction rather than a generic error", async () => {
    getMock.mockResolvedValue({
      dirty: true,
      changedFields: ["mission"],
      currentVersionNo: 3,
    });
    postMock.mockRejectedValue(apiError(409, { error: "stale_version", expected: 3, current: 5 }));
    renderBar(<AgentPublishBar agentId="ag-1" mayManage={true} />);
    fireEvent.click(await screen.findByRole("button", { name: /publish/i }));
    await waitFor(() => expect(toastError).toHaveBeenCalled());
    const description = String(toastError.mock.calls[0]?.[1]?.description ?? "");
    expect(description).toMatch(/v5/);
    expect(toastSuccess).not.toHaveBeenCalled();
  });

  it("opens the diff against the working copy when Review is clicked", async () => {
    getMock.mockImplementation((url: string) => {
      if (url.includes("draft-status")) {
        return Promise.resolve({
          dirty: true,
          changedFields: ["mission"],
          currentVersionNo: 3,
        });
      }
      if (url.includes("/versions/diff")) {
        return Promise.resolve({
          fromVersionNo: 3,
          toVersionNo: null,
          entries: [{ field: "mission", before: "old", after: "new" }],
        });
      }
      throw new Error(`unexpected GET ${url}`);
    });
    renderBar(<AgentPublishBar agentId="ag-1" mayManage={true} />);
    fireEvent.click(await screen.findByRole("button", { name: /review/i }));
    // `from` is the CURRENT version and `to` is omitted -- the right-hand side
    // is the unpublished draft.
    await waitFor(() => expect(getMock).toHaveBeenCalledWith("/agents/ag-1/versions/diff?from=3"));
    expect(await screen.findByText("mission")).toBeTruthy();
    expect(await screen.findByText("old")).toBeTruthy();
    expect(await screen.findByText("new")).toBeTruthy();
  });

  it("hides Publish from a caller who may not publish", async () => {
    getMock.mockResolvedValue({
      dirty: true,
      changedFields: ["mission"],
      currentVersionNo: 3,
    });
    mayMock.mockReturnValue(false);
    renderBar(<AgentPublishBar agentId="ag-1" mayManage={true} />);
    expect(await screen.findByRole("button", { name: /review/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /publish/i })).toBeNull();
  });

  it("hides Publish from a caller who may not write this agent", async () => {
    getMock.mockResolvedValue({
      dirty: true,
      changedFields: ["mission"],
      currentVersionNo: 3,
    });
    renderBar(<AgentPublishBar agentId="ag-1" mayManage={false} />);
    expect(await screen.findByRole("button", { name: /review/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /publish/i })).toBeNull();
  });
});
