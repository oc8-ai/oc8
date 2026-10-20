import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { API_URL, COMMUNITY_TOKEN_KEY, api, setSessionRenewer } from "./api";

// What a 401 does to the session. With no renewer set, `request()` signs out
// at once, exactly as before the seam existed. With one set, it asks the
// renewer first, shares that one attempt across every request that hit the
// 401 at the same moment, and retries each request once with the new token.

const reloadMock = vi.fn();
const originalLocation = window.location;

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function authHeader(call: unknown[]): string | undefined {
  const init = call[1] as RequestInit | undefined;
  return (init?.headers as Record<string, string> | undefined)?.authorization;
}

/** Answers `/auth/config` (hit by `logoutCommunity`) with no SSO logout, and
 *  every other route through `handler`. */
function stubFetch(handler: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/auth/config")) return json(200, { ssoLogoutUrl: null });
    return handler(url, init);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function apiCalls(fetchMock: ReturnType<typeof stubFetch>) {
  return fetchMock.mock.calls.filter((c) => !String(c[0]).endsWith("/auth/config"));
}

beforeEach(() => {
  window.localStorage.clear();
  window.localStorage.setItem(COMMUNITY_TOKEN_KEY, "old-token");
  reloadMock.mockReset();
  // jsdom's `location.reload` is not configurable -- replace the object.
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...originalLocation, reload: reloadMock, origin: "http://localhost" },
  });
});

afterEach(() => {
  setSessionRenewer(null);
  vi.unstubAllGlobals();
  Object.defineProperty(window, "location", { configurable: true, value: originalLocation });
});

describe("request() on a 401", () => {
  it("signs out at once when no renewer is set", async () => {
    const fetchMock = stubFetch(() => json(401, { detail: "expired" }));

    await expect(api.get("/me")).rejects.toThrow("session expired");

    expect(apiCalls(fetchMock)).toHaveLength(1);
    expect(window.localStorage.getItem(COMMUNITY_TOKEN_KEY)).toBeNull();
    expect(reloadMock).toHaveBeenCalledTimes(1);
  });

  it("retries once with the renewed token and does not sign out", async () => {
    const fetchMock = stubFetch((_url, init) => {
      const auth = (init?.headers as Record<string, string>).authorization;
      return auth === "Bearer new-token"
        ? json(200, { ok: true })
        : json(401, { detail: "expired" });
    });
    const renewer = vi.fn(async () => {
      window.localStorage.setItem(COMMUNITY_TOKEN_KEY, "new-token");
      return true;
    });
    setSessionRenewer(renewer);

    await expect(api.get<{ ok: boolean }>("/me")).resolves.toEqual({ ok: true });

    expect(renewer).toHaveBeenCalledTimes(1);
    const calls = apiCalls(fetchMock);
    expect(calls).toHaveLength(2);
    expect(authHeader(calls[0])).toBe("Bearer old-token");
    expect(authHeader(calls[1])).toBe("Bearer new-token");
    expect(String(calls[1][0])).toBe(`${API_URL}/me`);
    expect(window.localStorage.getItem(COMMUNITY_TOKEN_KEY)).toBe("new-token");
    expect(reloadMock).not.toHaveBeenCalled();
  });

  it("keeps the method and body on the retry", async () => {
    const fetchMock = stubFetch((_url, init) => {
      const auth = (init?.headers as Record<string, string>).authorization;
      return auth === "Bearer new-token" ? json(200, { ok: true }) : json(401, {});
    });
    setSessionRenewer(async () => {
      window.localStorage.setItem(COMMUNITY_TOKEN_KEY, "new-token");
      return true;
    });

    await api.post("/things", { name: "x" });

    const retry = apiCalls(fetchMock)[1][1] as RequestInit;
    expect(retry.method).toBe("POST");
    expect(retry.body).toBe(JSON.stringify({ name: "x" }));
  });

  it("signs out when the renewer resolves false", async () => {
    const fetchMock = stubFetch(() => json(401, { detail: "expired" }));
    const renewer = vi.fn(async () => false);
    setSessionRenewer(renewer);

    await expect(api.get("/me")).rejects.toThrow("session expired");

    expect(renewer).toHaveBeenCalledTimes(1);
    expect(apiCalls(fetchMock)).toHaveLength(1);
    expect(window.localStorage.getItem(COMMUNITY_TOKEN_KEY)).toBeNull();
    expect(reloadMock).toHaveBeenCalledTimes(1);
  });

  it("signs out when the renewer throws", async () => {
    stubFetch(() => json(401, { detail: "expired" }));
    setSessionRenewer(async () => {
      throw new Error("network down");
    });

    await expect(api.get("/me")).rejects.toThrow("session expired");

    expect(window.localStorage.getItem(COMMUNITY_TOKEN_KEY)).toBeNull();
    expect(reloadMock).toHaveBeenCalledTimes(1);
  });

  it("shares one renewal across concurrent 401s and retries each request", async () => {
    const fetchMock = stubFetch((_url, init) => {
      const auth = (init?.headers as Record<string, string>).authorization;
      return auth === "Bearer new-token"
        ? json(200, { ok: true })
        : json(401, { detail: "expired" });
    });
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    const renewer = vi.fn(async () => {
      await gate;
      window.localStorage.setItem(COMMUNITY_TOKEN_KEY, "new-token");
      return true;
    });
    setSessionRenewer(renewer);

    const pending = Promise.all([1, 2, 3, 4, 5].map((n) => api.get(`/things/${n}`)));
    // Let every first attempt come back 401 and queue on the renewal.
    await vi.waitFor(() => expect(apiCalls(fetchMock)).toHaveLength(5));
    release();

    await expect(pending).resolves.toHaveLength(5);
    expect(renewer).toHaveBeenCalledTimes(1);
    const retries = apiCalls(fetchMock).slice(5);
    expect(retries).toHaveLength(5);
    expect(retries.every((c) => authHeader(c) === "Bearer new-token")).toBe(true);
    expect(reloadMock).not.toHaveBeenCalled();
  });

  it("retries a late 401 with the already renewed token instead of renewing again", async () => {
    // A request sent with the old token comes back 401 only after another
    // request has already renewed the session.
    let releaseSlow!: () => void;
    const slow = new Promise<void>((resolve) => (releaseSlow = resolve));
    stubFetch(async (url, init) => {
      const auth = (init?.headers as Record<string, string>).authorization;
      if (auth === "Bearer new-token") return json(200, { url });
      if (url.endsWith("/slow")) await slow;
      return json(401, {});
    });
    const renewer = vi.fn(async () => {
      window.localStorage.setItem(COMMUNITY_TOKEN_KEY, "new-token");
      return true;
    });
    setSessionRenewer(renewer);

    const late = api.get("/slow");
    await api.get("/fast");
    releaseSlow();

    await expect(late).resolves.toEqual({ url: `${API_URL}/slow` });
    expect(renewer).toHaveBeenCalledTimes(1);
    expect(reloadMock).not.toHaveBeenCalled();
  });

  it("signs out when the renewer reports success but the token is unchanged", async () => {
    const fetchMock = stubFetch(() => json(401, {}));
    setSessionRenewer(async () => true);

    await expect(api.get("/me")).rejects.toThrow("session expired");

    expect(apiCalls(fetchMock)).toHaveLength(1);
    expect(reloadMock).toHaveBeenCalledTimes(1);
  });

  it("asks the renewer again for a later, separate 401", async () => {
    let valid = "new-token";
    stubFetch((_url, init) => {
      const auth = (init?.headers as Record<string, string>).authorization;
      return auth === `Bearer ${valid}` ? json(200, {}) : json(401, {});
    });
    let n = 0;
    const renewer = vi.fn(async () => {
      n += 1;
      window.localStorage.setItem(COMMUNITY_TOKEN_KEY, `token-${n}`);
      valid = `token-${n}`;
      return true;
    });
    setSessionRenewer(renewer);

    await api.get("/a");
    window.localStorage.setItem(COMMUNITY_TOKEN_KEY, "stale");
    await api.get("/b");

    expect(renewer).toHaveBeenCalledTimes(2);
  });

  it("signs out without looping when the retry also gets a 401", async () => {
    const fetchMock = stubFetch(() => json(401, { detail: "expired" }));
    const renewer = vi.fn(async () => {
      window.localStorage.setItem(COMMUNITY_TOKEN_KEY, "new-token");
      return true;
    });
    setSessionRenewer(renewer);

    await expect(api.get("/me")).rejects.toThrow("session expired");

    expect(renewer).toHaveBeenCalledTimes(1);
    expect(apiCalls(fetchMock)).toHaveLength(2);
    expect(window.localStorage.getItem(COMMUNITY_TOKEN_KEY)).toBeNull();
    expect(reloadMock).toHaveBeenCalledTimes(1);
  });
});
