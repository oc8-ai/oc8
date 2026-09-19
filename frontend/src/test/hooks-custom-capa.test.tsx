import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

const { postCapas } = vi.hoisted(() => ({
  postCapas: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    post: (path: string, body: unknown) =>
      path === "/capas" ? postCapas(body) : Promise.reject(new Error(`unexpected POST ${path}`)),
  },
}));

import { useInstallCustomCapa } from "@/lib/hooks";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("useInstallCustomCapa", () => {
  it("posts the manifest with origin=custom and returns the new pluginId", async () => {
    postCapas.mockResolvedValue({
      id: "v1",
      pluginId: "capa-1",
      name: "acme_billing",
      type: "tool_pack",
      semver: "1.0.0",
      trustLevel: "unverified",
    });

    const { result } = renderHook(() => useInstallCustomCapa(), { wrapper });
    result.current.mutate({ manifest: { name: "acme_billing", version: "1.0.0" } });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.pluginId).toBe("capa-1");

    // Verify the correct payload was sent
    expect(postCapas).toHaveBeenCalledWith({
      manifest: { name: "acme_billing", version: "1.0.0" },
      origin: "custom",
    });
  });
});
