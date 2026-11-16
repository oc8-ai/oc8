import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { NotificationBell } from "@/components/notification-bell";
import { api } from "@/lib/api";

const navigate = vi.fn();
vi.mock("@tanstack/react-router", () => ({ useNavigate: () => navigate }));
const toastError = vi.hoisted(() => vi.fn());
vi.mock("sonner", () => ({ toast: { error: toastError } }));
vi.mock("@/lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn() },
  retryUnlessRefused: () => false,
}));

const item = (id: string, read = false) => ({
  id,
  kind: "board.available",
  subjectId: "b1",
  title: { en: `Title ${id}`, de: `Titel ${id}` },
  body: { en: `Body ${id}`, de: `Text ${id}` },
  link: `/boards/${id}`,
  createdAt: new Date().toISOString(),
  read,
});

function setup(count: number, items: ReturnType<typeof item>[]) {
  vi.mocked(api.get).mockImplementation(async (path: string) =>
    path.startsWith("/notifications/unread-count")
      ? { count }
      : { items, totalCount: items.length, nextCursor: null },
  );
  vi.mocked(api.post).mockResolvedValue(undefined);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <NotificationBell />
    </QueryClientProvider>,
  );
}

beforeEach(() => vi.clearAllMocks());

describe("NotificationBell", () => {
  it("shows the unread count", async () => {
    setup(3, []);
    expect(await screen.findByText("3")).toBeTruthy();
  });

  it("caps the badge at 9+", async () => {
    setup(12, []);
    expect(await screen.findByText("9+")).toBeTruthy();
  });

  it("lists items, marks read and navigates on click", async () => {
    setup(1, [item("n1"), item("n2", true)]);
    fireEvent.pointerDown(await screen.findByLabelText("Notifications"), {
      button: 0,
      ctrlKey: false,
    });
    fireEvent.click(await screen.findByText("Title n1"));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/notifications/n1/read"));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith({ href: "/boards/n1" }));
  });

  it("does not navigate to an external or protocol-relative link", async () => {
    setup(1, [
      { ...item("n1"), link: "//evil.example/x" },
      { ...item("n2"), link: "https://evil.example" },
    ]);
    fireEvent.pointerDown(await screen.findByLabelText("Notifications"), {
      button: 0,
      ctrlKey: false,
    });
    fireEvent.click(await screen.findByText("Title n1"));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/notifications/n1/read"));
    expect(navigate).not.toHaveBeenCalled();
  });

  it("does not navigate to a backslash link", async () => {
    setup(1, [{ ...item("n3"), link: "/\\evil.example" }]);
    fireEvent.pointerDown(await screen.findByLabelText("Notifications"), {
      button: 0,
      ctrlKey: false,
    });
    fireEvent.click(await screen.findByText("Title n3"));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/notifications/n3/read"));
    expect(navigate).not.toHaveBeenCalled();
  });

  it("toasts when marking read fails", async () => {
    setup(1, [item("n1")]);
    vi.mocked(api.post).mockRejectedValue(new Error("boom"));
    fireEvent.pointerDown(await screen.findByLabelText("Notifications"), {
      button: 0,
      ctrlKey: false,
    });
    fireEvent.click(await screen.findByText("Title n1"));
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Couldn't mark the notification as read"),
    );
  });

  it("marks all as read", async () => {
    setup(1, [item("n1")]);
    fireEvent.pointerDown(await screen.findByLabelText("Notifications"), {
      button: 0,
      ctrlKey: false,
    });
    fireEvent.click(await screen.findByText("Mark all as read"));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/notifications/read-all"));
  });

  it("shows the empty state", async () => {
    setup(0, []);
    fireEvent.pointerDown(await screen.findByLabelText("Notifications"), {
      button: 0,
      ctrlKey: false,
    });
    expect(await screen.findByText("No notifications")).toBeTruthy();
  });
});
