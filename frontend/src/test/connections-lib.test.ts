import { createElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const post = vi.fn();
const put = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      post: (...args: unknown[]) => post(...args),
      put: (...args: unknown[]) => put(...args),
    },
  };
});

import { ApiError } from "@/lib/api";
import {
  OAuthCompleteError,
  connectionStateOf,
  useCompleteOAuth,
  useSetAgentToolAccess,
  useStartOAuth,
  type Connection,
  type Integration,
} from "@/lib/connections";

const MS: Integration = {
  name: "microsoft365",
  label: "Microsoft 365",
  summary: "",
  toolKey: "microsoft365",
  kind: "oauth",
  provider: "microsoft",
  credentialType: null,
  credentialFields: [],
  available: true,
  capaId: "c1",
};

function conn(over: Partial<Connection>): Connection {
  return {
    id: "x",
    kind: "oauth",
    integration: "microsoft365",
    provider: "microsoft",
    credentialType: null,
    accountLabel: "anna@acme.de",
    owner: "me",
    ownerMemberId: "m1",
    ownerName: null,
    status: "active",
    grantType: "authorization_code",
    createdAt: "2026-11-08T10:00:00Z",
    createdByName: null,
    lastTestedAt: null,
    lastTestOk: null,
    ...over,
  };
}

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client: qc }, children);
}

describe("connectionStateOf", () => {
  beforeEach(() => post.mockReset());

  it("is unavailable without an installed, available integration", () => {
    expect(connectionStateOf(undefined, []).state).toBe("unavailable");
    expect(connectionStateOf({ ...MS, available: false }, [conn({})]).state).toBe("unavailable");
  });

  it("counts only the caller's own rows of this integration", () => {
    const others = [conn({ owner: "member" }), conn({ owner: "organisation" }), conn({ integration: "google_workspace" })];
    expect(connectionStateOf(MS, others).state).toBe("not_connected");
    expect(connectionStateOf(MS, [...others, conn({})]).state).toBe("connected");
  });

  it("asks to reconnect when no own row is healthy", () => {
    expect(connectionStateOf(MS, [conn({ status: "needs_reauth" })]).state).toBe("needs_reauth");
    const odoo: Integration = { ...MS, name: "odoo_mcp", kind: "credential", provider: null, credentialType: "odoo_login" };
    const failed = conn({ kind: "credential", integration: "odoo_mcp", status: "failed" });
    expect(connectionStateOf(odoo, [failed]).state).toBe("needs_reauth");
    expect(connectionStateOf(odoo, [{ ...failed, status: "untested" }]).state).toBe("connected");
  });
});

describe("oauth hooks", () => {
  beforeEach(() => {
    post.mockReset();
    put.mockReset();
  });

  it("starts with scopeSet and owner", async () => {
    post.mockResolvedValue({ authorizationUrl: "https://idp/auth" });
    const { result } = renderHook(() => useStartOAuth(), { wrapper: wrapper() });
    await act(() => result.current.mutateAsync({ provider: "microsoft", owner: "organisation" }));
    expect(post).toHaveBeenCalledWith("/oauth/microsoft/start", { scopeSet: "tools", owner: "organisation" });
  });

  it("completes with the pending id and returns the connection", async () => {
    const c = conn({});
    post.mockResolvedValue({ connected: "microsoft", connection: c });
    const { result } = renderHook(() => useCompleteOAuth(), { wrapper: wrapper() });
    let out: unknown;
    await act(async () => {
      out = await result.current.mutateAsync({ provider: "microsoft", pending: "p1" });
    });
    expect(post).toHaveBeenCalledWith("/oauth/microsoft/complete", { pending: "p1" });
    expect(out).toEqual(c);
  });

  it("surfaces a typed error with the admin consent url", async () => {
    post.mockRejectedValue(
      new ApiError("admin_consent_required", 400, { error: "admin_consent_required", adminConsentUrl: "https://ms/consent" }),
    );
    const { result } = renderHook(() => useCompleteOAuth(), { wrapper: wrapper() });
    await act(async () => {
      await result.current.mutateAsync({ provider: "microsoft", pending: "p1" }).catch(() => {});
    });
    await waitFor(() => expect(result.current.error).toBeInstanceOf(OAuthCompleteError));
    expect(result.current.error).toMatchObject({ code: "admin_consent_required", adminConsentUrl: "https://ms/consent" });
  });

  it("keeps a non-typed failure as it is", async () => {
    const boom = new ApiError("no pending connection", 404, { detail: "no pending connection" });
    post.mockRejectedValue(boom);
    const { result } = renderHook(() => useCompleteOAuth(), { wrapper: wrapper() });
    await act(async () => {
      await result.current.mutateAsync({ provider: "google", pending: "p1" }).catch(() => {});
    });
    await waitFor(() => expect(result.current.error).toBe(boom));
  });

  it("sets a tool access with confirmShare", async () => {
    put.mockResolvedValue({});
    const { result } = renderHook(() => useSetAgentToolAccess("a1"), { wrapper: wrapper() });
    await act(() =>
      result.current.mutateAsync({
        toolKey: "microsoft365",
        body: { mode: "fixed", oauthConnectionId: "c1", confirmShare: true },
      }),
    );
    expect(put).toHaveBeenCalledWith("/agents/a1/tool-access/microsoft365", {
      mode: "fixed",
      oauthConnectionId: "c1",
      confirmShare: true,
    });
  });
});
