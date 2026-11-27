import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it, vi } from "vitest";

// Runs the real public/sw.js -- the file the browser registers -- against a
// fake worker global, so there is no second copy of the cache rules to drift
// from the shipped one.
const SOURCE = readFileSync(resolve(process.cwd(), "public/sw.js"), "utf8");
const ORIGIN = "https://oc8.example.com";

type Listener = (event: Record<string, unknown>) => void;

interface FakeRequest {
  url: string;
  method: string;
  mode: string;
  headers: Headers;
}

function request(path: string, init: Partial<Omit<FakeRequest, "url">> = {}): FakeRequest {
  return {
    url: path.startsWith("http") ? path : `${ORIGIN}${path}`,
    method: init.method ?? "GET",
    mode: init.mode ?? "cors",
    headers: init.headers ?? new Headers(),
  };
}

interface FakeResponse {
  ok: boolean;
  status: number;
  type: string;
  clone: () => unknown;
}

function response(status = 200, type = "basic"): FakeResponse {
  return { ok: status >= 200 && status < 300, status, type, clone: () => "copy" };
}

/** Which Cache Storage call fails, the way Safari/Chrome fail them on a full
 *  or restricted phone (QuotaExceededError, SecurityError, Vary: *). */
type Failing = "open" | "match" | "put";

function fakeCacheStorage(initial: string[] = [], failing?: Failing) {
  const stores = new Map<string, Map<string, unknown>>(initial.map((name) => [name, new Map()]));
  const fail = (op: Failing) => {
    if (failing === op) throw new DOMException(`${op} failed`, "QuotaExceededError");
  };
  return {
    stores,
    keys: vi.fn(async () => [...stores.keys()]),
    delete: vi.fn(async (name: string) => stores.delete(name)),
    open: vi.fn(async (name: string) => {
      fail("open");
      if (!stores.has(name)) stores.set(name, new Map());
      const store = stores.get(name)!;
      return {
        match: vi.fn(async (req: FakeRequest) => {
          fail("match");
          return store.get(req.url);
        }),
        put: vi.fn(async (req: FakeRequest, res: unknown) => {
          fail("put");
          store.set(req.url, res);
        }),
        // Insertion order, as the Cache API returns them.
        keys: vi.fn(async () => [...store.keys()].map((url) => ({ url }))),
        delete: vi.fn(async (req: { url: string }) => store.delete(req.url)),
      };
    }),
  };
}

function loadWorker(
  opts: { caches?: ReturnType<typeof fakeCacheStorage>; clients?: unknown[] } = {},
) {
  const listeners = new Map<string, Listener>();
  const caches = opts.caches ?? fakeCacheStorage();
  const clientsList = opts.clients ?? [];
  const self = {
    location: new URL(`${ORIGIN}/sw.js`),
    addEventListener: (type: string, fn: Listener) => listeners.set(type, fn),
    skipWaiting: vi.fn(async () => {}),
    clients: {
      matchAll: vi.fn(async () => clientsList),
      claim: vi.fn(async () => {}),
      openWindow: vi.fn(async () => {}),
    },
    registration: { showNotification: vi.fn(async () => {}) },
  };
  const network = vi.fn(async (): Promise<FakeResponse> => response());
  const exports = new Function(
    "self",
    "caches",
    "fetch",
    `${SOURCE}\n;return { isCacheableRequest, ASSET_CACHE, MAX_ASSETS };`,
  )(self, caches, network) as {
    isCacheableRequest: (req: FakeRequest, origin: string) => boolean;
    ASSET_CACHE: string;
    MAX_ASSETS: number;
  };

  async function dispatch(type: string, event: Record<string, unknown>) {
    const pending: Promise<unknown>[] = [];
    let responded: Promise<unknown> | undefined;
    listeners.get(type)?.({
      ...event,
      waitUntil: (p: Promise<unknown>) => pending.push(p),
      respondWith: (p: Promise<unknown>) => {
        responded = Promise.resolve(p);
      },
    });
    // The answer first: storing a copy is extended work (waitUntil) that the
    // worker may start only once the response is on its way.
    let result: unknown;
    let failure: unknown;
    if (responded) {
      try {
        result = await responded;
      } catch (error) {
        failure = error ?? new Error("rejected");
      }
    }
    for (let i = 0; i < pending.length; i++) await pending[i];
    if (failure) throw failure;
    return result;
  }

  return { ...exports, listeners, caches, self, network, dispatch };
}

describe("service worker cache rules", () => {
  const { isCacheableRequest } = loadWorker();
  const ok = (req: FakeRequest) => isCacheableRequest(req, ORIGIN);

  it("accepts same-origin GETs of content-hashed build assets", () => {
    expect(ok(request("/assets/index-BQhdFMY1.js"))).toBe(true);
    expect(ok(request("/assets/styles-Dq_IR9rO.css"))).toBe(true);
    expect(ok(request("/assets/KaTeX_Caligraphic-Regular-CTRA-rTL.woff"))).toBe(true);
  });

  it("rejects anything that is not a hashed file under /assets/", () => {
    expect(ok(request("/assets/logo.png"))).toBe(false);
    expect(ok(request("/assets/nested/index-BQhdFMY1.js"))).toBe(false);
    expect(ok(request("/assets/index-BQhdFMY1.js?v=2"))).toBe(false);
    expect(ok(request("/favicon-192.png"))).toBe(false);
    expect(ok(request("/sw.js"))).toBe(false);
    expect(ok(request("/manifest.webmanifest"))).toBe(false);
  });

  it("never caches the API, model or tool endpoints", () => {
    expect(ok(request("/api/v1/chat/sessions"))).toBe(false);
    expect(ok(request("/llm/v1/chat/completions"))).toBe(false);
    expect(ok(request("/mcp/tools"))).toBe(false);
    expect(ok(request("/health"))).toBe(false);
    // A look-alike path must not slip through either.
    expect(ok(request("/api/assets/index-BQhdFMY1.js"))).toBe(false);
  });

  it("leaves Pyodide to the HTTP cache", () => {
    expect(ok(request("/pyodide/pyodide.asm.wasm"))).toBe(false);
    expect(ok(request("/pyodide/pyodide-lock.json"))).toBe(false);
  });

  it("rejects navigations, other origins, other methods and authorized requests", () => {
    expect(ok(request("/assets/index-BQhdFMY1.js", { mode: "navigate" }))).toBe(false);
    expect(ok(request("https://cdn.example.com/assets/index-BQhdFMY1.js"))).toBe(false);
    expect(ok(request("/assets/index-BQhdFMY1.js", { method: "POST" }))).toBe(false);
    expect(
      ok(
        request("/assets/index-BQhdFMY1.js", {
          headers: new Headers({ Authorization: "Bearer secret" }),
        }),
      ),
    ).toBe(false);
  });

  it("skips range requests (a 206 is a slice, not the file)", () => {
    expect(
      ok(request("/assets/index-BQhdFMY1.js", { headers: new Headers({ Range: "bytes=0-99" }) })),
    ).toBe(false);
  });

  it("rejects encoded or dot-segment paths that only look like /assets/", () => {
    expect(ok(request("/assets/../api/x-abcdefgh.js"))).toBe(false);
    expect(ok(request("/assets/..%2Fapi%2Fx-abcdefgh.js"))).toBe(false);
    expect(ok(request("/assets/x%2Dabcdefgh.js"))).toBe(false);
    expect(ok(request("/assets/a..b-abcdefgh.js"))).toBe(false);
  });
});

describe("service worker fetch handling", () => {
  it("serves a hashed asset from the network once, then from the cache", async () => {
    const worker = loadWorker();
    const req = request("/assets/index-BQhdFMY1.js");

    const first = await worker.dispatch("fetch", { request: req });
    expect(first).toMatchObject({ ok: true });
    expect(worker.network).toHaveBeenCalledTimes(1);
    expect(worker.caches.stores.get(worker.ASSET_CACHE)?.get(req.url)).toBe("copy");

    const second = await worker.dispatch("fetch", { request: req });
    expect(second).toBe("copy");
    expect(worker.network).toHaveBeenCalledTimes(1);
  });

  it("does not answer requests outside the rules, so the browser fetches them as usual", async () => {
    const worker = loadWorker();
    const answered = await worker.dispatch("fetch", { request: request("/api/v1/me") });
    expect(answered).toBeUndefined();
    expect(worker.network).not.toHaveBeenCalled();
    expect(worker.caches.open).not.toHaveBeenCalled();
  });

  it("does not store failed responses", async () => {
    const worker = loadWorker();
    worker.network.mockResolvedValueOnce(response(404));
    await worker.dispatch("fetch", { request: request("/assets/index-BQhdFMY1.js") });
    expect(worker.caches.stores.get(worker.ASSET_CACHE)?.size ?? 0).toBe(0);
  });

  it("stores only a complete 200, never a 206 or an opaque response", async () => {
    for (const res of [response(206), response(200, "opaque"), response(200, "cors")]) {
      const worker = loadWorker();
      worker.network.mockResolvedValueOnce(res);
      const answered = await worker.dispatch("fetch", {
        request: request("/assets/index-BQhdFMY1.js"),
      });
      expect(answered).toBe(res);
      expect(worker.caches.stores.get(worker.ASSET_CACHE)?.size ?? 0).toBe(0);
    }
  });

  it.each<Failing>(["open", "match", "put"])(
    "still serves the network response when Cache Storage fails at %s",
    async (failing) => {
      const worker = loadWorker({ caches: fakeCacheStorage([], failing) });
      const answered = await worker.dispatch("fetch", {
        request: request("/assets/index-BQhdFMY1.js"),
      });
      expect(answered).toMatchObject({ status: 200 });
      expect(worker.network).toHaveBeenCalledTimes(1);
    },
  );

  it("lets a network failure through instead of inventing a response", async () => {
    const worker = loadWorker();
    worker.network.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await expect(
      worker.dispatch("fetch", { request: request("/assets/index-BQhdFMY1.js") }),
    ).rejects.toThrow("Failed to fetch");
  });

  it("keeps at most MAX_ASSETS files, dropping the oldest", async () => {
    const worker = loadWorker();
    expect(worker.MAX_ASSETS).toBe(200);
    const store = new Map<string, unknown>();
    for (let i = 0; i < worker.MAX_ASSETS; i++)
      store.set(`${ORIGIN}/assets/old${i}-abcdefgh.js`, "x");
    worker.caches.stores.set(worker.ASSET_CACHE, store);

    await worker.dispatch("fetch", { request: request("/assets/index-BQhdFMY1.js") });

    expect(store.size).toBe(worker.MAX_ASSETS);
    expect(store.has(`${ORIGIN}/assets/old0-abcdefgh.js`)).toBe(false);
    expect(store.has(`${ORIGIN}/assets/old1-abcdefgh.js`)).toBe(true);
    expect(store.has(`${ORIGIN}/assets/index-BQhdFMY1.js`)).toBe(true);
  });
});

describe("service worker lifecycle and purge", () => {
  it("uses a versioned cache name and drops older caches on activate", async () => {
    const worker = loadWorker({ caches: fakeCacheStorage(["oc8-assets-v0", "something-else"]) });
    expect(worker.ASSET_CACHE).toMatch(/^oc8-assets-v\d+$/);
    worker.caches.stores.set(worker.ASSET_CACHE, new Map());

    await worker.dispatch("activate", {});

    expect([...worker.caches.stores.keys()]).toEqual([worker.ASSET_CACHE]);
    expect(worker.self.clients.claim).toHaveBeenCalled();
  });

  it("deletes every cache when told to purge", async () => {
    const worker = loadWorker({ caches: fakeCacheStorage(["oc8-assets-v1", "oc8-assets-v0"]) });
    await worker.dispatch("message", { data: { type: "purge" } });
    expect(worker.caches.stores.size).toBe(0);
  });

  it("ignores other messages", async () => {
    const worker = loadWorker({ caches: fakeCacheStorage(["oc8-assets-v1"]) });
    await worker.dispatch("message", { data: { type: "something" } });
    expect(worker.caches.stores.size).toBe(1);
  });
});

describe("service worker push handling (unchanged)", () => {
  it("still listens for push, notification clicks and subscription rotation", () => {
    const worker = loadWorker();
    expect(worker.listeners.has("push")).toBe(true);
    expect(worker.listeners.has("notificationclick")).toBe(true);
    expect(worker.listeners.has("pushsubscriptionchange")).toBe(true);
  });

  it("shows a notification when no tab is focused", async () => {
    const worker = loadWorker({ clients: [{ focused: false }] });
    await worker.dispatch("push", {
      data: { json: () => ({ title: "Approval", body: "Needs you", url: "/approvals" }) },
    });
    expect(worker.self.registration.showNotification).toHaveBeenCalledWith("Approval", {
      body: "Needs you",
      data: { url: "/approvals" },
    });
  });

  it("stays quiet when a tab is focused (the in-app toast covers it)", async () => {
    const worker = loadWorker({ clients: [{ focused: true }] });
    await worker.dispatch("push", {
      data: { json: () => ({ title: "Approval", body: "Needs you", url: "/approvals" }) },
    });
    expect(worker.self.registration.showNotification).not.toHaveBeenCalled();
  });
});
