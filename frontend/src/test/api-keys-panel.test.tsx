import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { ApiKeysPanel } from "@/components/api-keys-panel";
import type { ApiKeyDTO, ApiKeyCreatedDTO } from "@/lib/hooks";

// vi.hoisted: `vi.mock` factories are hoisted above ordinary `const`s by
// vitest -- see the identical pattern in settings.test.tsx/backup-panel.test.tsx.
const { getKeysMock, createKeyMock, updateKeyMock, deleteKeyMock } = vi.hoisted(() => ({
  getKeysMock: vi.fn(),
  createKeyMock: vi.fn(),
  updateKeyMock: vi.fn(),
  deleteKeyMock: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    get: (path: string) => {
      if (path === "/settings/api-keys") return getKeysMock();
      return Promise.reject(new Error(`unexpected GET ${path}`));
    },
    post: (path: string, body: unknown) => {
      if (path === "/settings/api-keys") return createKeyMock(body);
      return Promise.reject(new Error(`unexpected POST ${path}`));
    },
    patch: (path: string, body: unknown) => {
      const match = /^\/settings\/api-keys\/([^/]+)$/.exec(path);
      if (match) return updateKeyMock(match[1], body);
      return Promise.reject(new Error(`unexpected PATCH ${path}`));
    },
    delete: (path: string) => {
      const match = /^\/settings\/api-keys\/([^/]+)$/.exec(path);
      if (match) return deleteKeyMock(match[1]);
      return Promise.reject(new Error(`unexpected DELETE ${path}`));
    },
  },
}));

const { toastErrorMock, toastSuccessMock } = vi.hoisted(() => ({
  toastErrorMock: vi.fn(),
  toastSuccessMock: vi.fn(),
}));
vi.mock("sonner", async (importOriginal) => {
  const actual = await importOriginal<typeof import("sonner")>();
  return {
    ...actual,
    toast: { ...actual.toast, error: toastErrorMock, success: toastSuccessMock },
  };
});

const KEY: ApiKeyDTO = {
  id: "k1",
  name: "Claude Desktop",
  tokenPrefix: "ab12",
  enabled: true,
  allowedOrigins: [],
  lastUsedAt: null,
  createdAt: "2026-08-27T00:00:00Z",
};

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ApiKeysPanel />
    </QueryClientProvider>,
  );
}

describe("ApiKeysPanel", () => {
  beforeEach(() => {
    getKeysMock.mockReset();
    createKeyMock.mockReset();
    updateKeyMock.mockReset();
    deleteKeyMock.mockReset();
    toastErrorMock.mockReset();
    toastSuccessMock.mockReset();
  });

  it("shows an empty state when the caller has no keys yet", async () => {
    getKeysMock.mockResolvedValue([]);
    renderPanel();
    expect(await screen.findByText("No API keys yet.")).toBeInTheDocument();
  });

  it("lists existing keys with their prefix and last-used status", async () => {
    getKeysMock.mockResolvedValue([KEY]);
    renderPanel();
    expect(await screen.findByText("Claude Desktop")).toBeInTheDocument();
    expect(screen.getByText("oc8_ak_ab12…")).toBeInTheDocument();
    expect(screen.getByText(/never used/i)).toBeInTheDocument();
  });

  it("creates a key and reveals the one-time token", async () => {
    getKeysMock.mockResolvedValue([]);
    const created: ApiKeyCreatedDTO = { ...KEY, token: "oc8_ak_ab12cdefgh" };
    createKeyMock.mockResolvedValue(created);
    renderPanel();

    await screen.findByText("No API keys yet.");
    fireEvent.click(screen.getByRole("button", { name: /new key/i }));
    fireEvent.change(screen.getByPlaceholderText("e.g. Claude Desktop"), {
      target: { value: "Claude Desktop" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^create$/i }));

    await waitFor(() =>
      expect(createKeyMock).toHaveBeenCalledWith({ name: "Claude Desktop", allowedOrigins: [] }),
    );
    expect(
      await screen.findByText("Copy this key now — it will not be shown again."),
    ).toBeInTheDocument();
    expect(screen.getByText("oc8_ak_ab12cdefgh")).toBeInTheDocument();
  });

  it("splits comma-separated allowed origins into a trimmed list", async () => {
    getKeysMock.mockResolvedValue([]);
    createKeyMock.mockResolvedValue({ ...KEY, token: "oc8_ak_x" });
    renderPanel();

    await screen.findByText("No API keys yet.");
    fireEvent.click(screen.getByRole("button", { name: /new key/i }));
    fireEvent.change(screen.getByPlaceholderText("e.g. Claude Desktop"), {
      target: { value: "n8n" },
    });
    fireEvent.change(screen.getByPlaceholderText("https://claude.ai"), {
      target: { value: "https://a.com, https://b.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^create$/i }));

    await waitFor(() =>
      expect(createKeyMock).toHaveBeenCalledWith({
        name: "n8n",
        allowedOrigins: ["https://a.com", "https://b.com"],
      }),
    );
  });

  it("toggles a key's enabled state", async () => {
    getKeysMock.mockResolvedValue([KEY]);
    updateKeyMock.mockResolvedValue({ ...KEY, enabled: false });
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /enabled/i }));

    await waitFor(() => expect(updateKeyMock).toHaveBeenCalledWith("k1", { enabled: false }));
  });

  it("deletes a key", async () => {
    getKeysMock.mockResolvedValue([KEY]);
    deleteKeyMock.mockResolvedValue(undefined);
    renderPanel();

    await screen.findByText("Claude Desktop");
    fireEvent.click(screen.getByRole("button", { name: "" }));

    await waitFor(() => expect(deleteKeyMock).toHaveBeenCalledWith("k1"));
    await waitFor(() => expect(toastSuccessMock).toHaveBeenCalledWith("API key deleted"));
  });

  it("shows an error toast when creation fails", async () => {
    getKeysMock.mockResolvedValue([]);
    createKeyMock.mockRejectedValue(new Error("Name already in use"));
    renderPanel();

    await screen.findByText("No API keys yet.");
    fireEvent.click(screen.getByRole("button", { name: /new key/i }));
    fireEvent.change(screen.getByPlaceholderText("e.g. Claude Desktop"), {
      target: { value: "dup" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^create$/i }));

    await waitFor(() => expect(toastErrorMock).toHaveBeenCalledWith("Name already in use"));
  });
});
