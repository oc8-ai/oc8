import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ToolScopesDialog } from "@/components/tool-scopes-dialog";

const { getMock, putMock } = vi.hoisted(() => ({ getMock: vi.fn(), putMock: vi.fn() }));
vi.mock("@/lib/api", () => ({ api: { get: getMock, put: putMock } }));

const SNAPSHOT = {
  connectionId: "c1",
  name: "shared_mailboxes",
  tools: ["mail_create_draft", "mail_get", "mail_list_folders", "mail_search"],
  read: [] as string[],
  modify: [] as string[],
  editable: true,
};

function renderDialog(onClose = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <ToolScopesDialog connectionId="c1" onClose={onClose} />
    </QueryClientProvider>,
  );
  return onClose;
}

describe("ToolScopesDialog", () => {
  beforeEach(() => {
    getMock.mockReset();
    putMock.mockReset();
  });

  it("lists every tool, and one not yet classified counts as modifying", async () => {
    getMock.mockResolvedValue(SNAPSHOT);
    renderDialog();
    for (const tool of SNAPSHOT.tools) {
      expect(await screen.findByText(tool)).toBeInTheDocument();
    }
    expect(getMock).toHaveBeenCalledWith("/mcp/connections/c1/tool-scopes");
    expect(screen.getByRole("checkbox", { name: /mail_search/ })).not.toBeChecked();
  });

  it("saves the tools ticked as read-only and every other one as modifying", async () => {
    getMock.mockResolvedValue(SNAPSHOT);
    putMock.mockResolvedValue({ ...SNAPSHOT, read: ["mail_get", "mail_search"] });
    renderDialog();
    fireEvent.click(await screen.findByRole("checkbox", { name: /mail_search/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /mail_get/ }));
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    await waitFor(() =>
      expect(putMock).toHaveBeenCalledWith("/mcp/connections/c1/tool-scopes", {
        read: ["mail_get", "mail_search"],
        modify: ["mail_create_draft", "mail_list_folders"],
      }),
    );
    expect(await screen.findByText(/saved/i)).toBeInTheDocument();
  });

  it("starts from the classification already saved", async () => {
    getMock.mockResolvedValue({
      ...SNAPSHOT,
      read: ["mail_get"],
      modify: ["mail_create_draft"],
    });
    renderDialog();
    expect(await screen.findByRole("checkbox", { name: /mail_get/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /mail_create_draft/ })).not.toBeChecked();
  });

  it("says when the server offered no tools yet", async () => {
    getMock.mockResolvedValue({ ...SNAPSHOT, tools: [] });
    renderDialog();
    expect(await screen.findByText(/test the connection/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^save$/i })).not.toBeInTheDocument();
  });

  it("shows why a save was refused", async () => {
    getMock.mockResolvedValue(SNAPSHOT);
    putMock.mockRejectedValue(new Error("not a tool of this connection: x"));
    renderDialog();
    fireEvent.click(await screen.findByRole("button", { name: /^save$/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/not a tool/);
  });
});
