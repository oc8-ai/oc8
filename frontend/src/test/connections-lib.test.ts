import { createElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const post = vi.fn();
const put = vi.fn();
const patch = vi.fn();
const del = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      post: (...args: unknown[]) => post(...args),
      put: (...args: unknown[]) => put(...args),
      patch: (...args: unknown[]) => patch(...args),
      delete: (...args: unknown[]) => del(...args),
    },
  };
});

import { ApiError } from "@/lib/api";
import {
  OAuthCompleteError,
  connectionStateOf,
  useCheckCredential,
  useCompleteOAuth,
  useCreateCredentialConnection,
  useDisconnect,
  useTestConnection,
  useUpdateOrganisationCredential,
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

function makeClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function wrapper(qc: QueryClient = makeClient()) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children);
}

describe("connectionStateOf", () => {
  beforeEach(() => post.mockReset());

  it("is unavailable without an installed, available integration", () => {
    expect(connectionStateOf(undefined, []).state).toBe("unavailable");
    expect(connectionStateOf({ ...MS, available: false }, [conn({})]).state).toBe("unavailable");
  });

  it("counts only the caller's own rows of this integration", () => {
    const others = [
      conn({ owner: "member" }),
      conn({ owner: "organisation" }),
      conn({ integration: "google_workspace" }),
    ];
    expect(connectionStateOf(MS, others).state).toBe("not_connected");
    expect(connectionStateOf(MS, [...others, conn({})]).state).toBe("connected");
  });

  it("asks to reconnect when no own row is healthy", () => {
    expect(connectionStateOf(MS, [conn({ status: "needs_reauth" })]).state).toBe("needs_reauth");
    const odoo: Integration = {
      ...MS,
      name: "odoo_mcp",
      kind: "credential",
      provider: null,
      credentialType: "odoo_login",
    };
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
    expect(post).toHaveBeenCalledWith("/oauth/microsoft/start", {
      scopeSet: "tools",
      owner: "organisation",
    });
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
      new ApiError("admin_consent_required", 400, {
        error: "admin_consent_required",
        adminConsentUrl: "https://ms/consent",
      }),
    );
    const { result } = renderHook(() => useCompleteOAuth(), { wrapper: wrapper() });
    await act(async () => {
      await result.current.mutateAsync({ provider: "microsoft", pending: "p1" }).catch(() => {});
    });
    await waitFor(() => expect(result.current.error).toBeInstanceOf(OAuthCompleteError));
    expect(result.current.error).toMatchObject({
      code: "admin_consent_required",
      adminConsentUrl: "https://ms/consent",
    });
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

describe("connection mutations", () => {
  beforeEach(() => {
    post.mockReset();
    put.mockReset();
    patch.mockReset();
    del.mockReset();
  });

  async function run<T>(hook: () => T, call: (h: T) => Promise<unknown>) {
    const { result } = renderHook(hook, { wrapper: wrapper() });
    await act(async () => {
      await call(result.current);
    });
  }

  it("uses the right method and url for each mutation", async () => {
    del.mockResolvedValue(undefined);
    post.mockResolvedValue({});
    patch.mockResolvedValue({});
    await run(useDisconnect, (h) => h.mutateAsync({ id: "o1", kind: "oauth" }));
    expect(del).toHaveBeenLastCalledWith("/connections/oauth/o1");
    await run(useDisconnect, (h) => h.mutateAsync({ id: "c1", kind: "credential" }));
    expect(del).toHaveBeenLastCalledWith("/connections/credentials/c1");
    const body = { integration: "odoo_mcp", owner: "me" as const, fieldValues: { a: "b" } };
    await run(useCreateCredentialConnection, (h) => h.mutateAsync(body));
    expect(post).toHaveBeenLastCalledWith("/connections/credentials", body);
    await run(useUpdateOrganisationCredential, (h) =>
      h.mutateAsync({ id: "c1", fieldValues: { a: "b" } }),
    );
    expect(patch).toHaveBeenLastCalledWith("/credentials/c1", { fieldValues: { a: "b" } });
    await run(useTestConnection, (h) => h.mutateAsync("c1"));
    expect(post).toHaveBeenLastCalledWith("/connections/credentials/c1/test");
    await run(useCheckCredential, (h) =>
      h.mutateAsync({ integration: "odoo_mcp", fieldValues: {} }),
    );
    expect(post).toHaveBeenLastCalledWith("/connections/credentials/check", {
      integration: "odoo_mcp",
      fieldValues: {},
    });
  });

  it("refreshes connections and agent tool access after disconnect", async () => {
    del.mockResolvedValue(undefined);
    const qc = makeClient();
    qc.setQueryData(["connections", "mine"], []);
    qc.setQueryData(["agents", "a1", "tool-access"], []);
    qc.setQueryData(["agents", "a1", "tool-access", "microsoft365", "options"], []);
    qc.setQueryData(["agents", "a1", "other"], []);
    const { result } = renderHook(() => useDisconnect(), { wrapper: wrapper(qc) });
    await act(() => result.current.mutateAsync({ id: "o1", kind: "oauth" }));
    expect(qc.getQueryState(["connections", "mine"])?.isInvalidated).toBe(true);
    expect(qc.getQueryState(["agents", "a1", "tool-access"])?.isInvalidated).toBe(true);
    expect(
      qc.getQueryState(["agents", "a1", "tool-access", "microsoft365", "options"])?.isInvalidated,
    ).toBe(true);
    expect(qc.getQueryState(["agents", "a1", "other"])?.isInvalidated).toBe(false);
  });

  it("refreshes connections and agent tool access after completing OAuth", async () => {
    post.mockResolvedValue({ connected: "microsoft", connection: conn({}) });
    const qc = makeClient();
    qc.setQueryData(["connections", "mine"], []);
    qc.setQueryData(["agents", "a1", "tool-access", "microsoft365", "options"], []);
    const { result } = renderHook(() => useCompleteOAuth(), { wrapper: wrapper(qc) });
    await act(() => result.current.mutateAsync({ provider: "microsoft", pending: "p1" }));
    expect(qc.getQueryState(["connections", "mine"])?.isInvalidated).toBe(true);
    expect(
      qc.getQueryState(["agents", "a1", "tool-access", "microsoft365", "options"])?.isInvalidated,
    ).toBe(true);
  });
});
