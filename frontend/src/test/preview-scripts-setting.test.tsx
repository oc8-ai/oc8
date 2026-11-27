// Settings: the tenant's "Run scripts in previews" switch (GET/PUT
// /settings/previews). Off, every artifact preview in the chat is static.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn() }));
const may = vi.hoisted(() => ({ manage: true }));
vi.mock("@/lib/api", () => ({ api }));
vi.mock("@/lib/governance-hooks", () => ({ useMay: () => () => may.manage }));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { PreviewScriptsSetting } from "@/components/settings/preview-scripts-setting";

function show() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <PreviewScriptsSetting />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  api.get.mockReset().mockResolvedValue({ runScripts: true });
  api.put.mockReset().mockImplementation(async (_path: string, body: unknown) => body);
  may.manage = true;
});

describe("PreviewScriptsSetting", () => {
  it("shows the setting and turns it off", async () => {
    show();
    const toggle = await screen.findByRole("switch", { name: "Run scripts in previews" });
    await waitFor(() => expect(toggle).toBeEnabled());
    expect(toggle).toBeChecked();
    expect(api.get).toHaveBeenCalledWith("/settings/previews");
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith("/settings/previews", { runScripts: false }),
    );
    await waitFor(() => expect(toggle).not.toBeChecked());
  });

  it("is read-only without settings:manage", async () => {
    may.manage = false;
    show();
    const toggle = await screen.findByRole("switch", { name: "Run scripts in previews" });
    await waitFor(() => expect(api.get).toHaveBeenCalled());
    expect(toggle).toBeDisabled();
  });
});
