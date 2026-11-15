// A grant changes what the connector catalog and the capas listing say about
// the capa, so both are fetched again.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

const postMock = vi.fn();
vi.mock("@/lib/api", () => ({
  api: { post: (path: string, body: unknown) => postMock(path, body) },
}));

import { useGrantCapaPermission } from "@/lib/knowledge-connector-hooks";

describe("useGrantCapaPermission", () => {
  it("posts the grant and refreshes the connector catalog and the capas listing", async () => {
    postMock.mockResolvedValue({ pluginId: "capa-1", status: "enabled", grantedPermissions: [] });
    const qc = new QueryClient();
    const invalidate = vi.spyOn(qc, "invalidateQueries");
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useGrantCapaPermission(), { wrapper });

    await act(() =>
      result.current.mutateAsync({ capaId: "capa-1", permission: "knowledge:write" }),
    );

    expect(postMock).toHaveBeenCalledWith("/capas/capa-1/grant", { permission: "knowledge:write" });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["knowledge", "connectors"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["plugins", "available"] });
  });
});
