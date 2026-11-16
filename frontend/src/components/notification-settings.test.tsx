import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { NotificationSettings } from "@/components/notification-settings";
import { api } from "@/lib/api";

const toastError = vi.fn();
vi.mock("sonner", () => ({ toast: { error: (msg: string) => toastError(msg) } }));

vi.mock("@/lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn() },
  retryUnlessRefused: () => false,
}));

const settings = [
  { kind: "board.available", pushEnabled: true, messengerEnabled: true },
  { kind: "run.failed", pushEnabled: true, messengerEnabled: true },
  { kind: "approval.decided", pushEnabled: true, messengerEnabled: true },
];

describe("NotificationSettings", () => {
  it("sends a PUT with the changed value", async () => {
    vi.mocked(api.get).mockResolvedValue(settings);
    vi.mocked(api.put).mockResolvedValue(settings);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <NotificationSettings />
      </QueryClientProvider>,
    );
    const sw = await screen.findByLabelText("Failed agents: Push");
    await waitFor(() => expect((sw as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(sw);
    await waitFor(() => expect(api.put).toHaveBeenCalled());
    expect(api.put).toHaveBeenCalledWith("/notifications/settings", [
      settings[0],
      { kind: "run.failed", pushEnabled: false, messengerEnabled: true },
      settings[2],
    ]);
  });

  it("says so when the save fails", async () => {
    vi.mocked(api.get).mockResolvedValue(settings);
    vi.mocked(api.put).mockRejectedValue(new Error("network down"));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <NotificationSettings />
      </QueryClientProvider>,
    );
    const sw = await screen.findByLabelText("Failed agents: Push");
    await waitFor(() => expect((sw as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(sw);
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Could not save your notification settings"),
    );
  });
});
