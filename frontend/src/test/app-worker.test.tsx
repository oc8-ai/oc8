import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { logoutCommunity, logoutDev } from "@/lib/api";
import { registerAppWorker } from "@/lib/app-worker";

function stubServiceWorker() {
  const registration = {
    pushManager: {
      subscribe: vi.fn(async () => ({
        toJSON: () => ({ endpoint: "https://push.example/1", keys: { p256dh: "p", auth: "a" } }),
      })),
    },
  };
  const controller = { postMessage: vi.fn() };
  const serviceWorker = {
    register: vi.fn(async () => registration),
    ready: Promise.resolve(registration),
    controller,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  };
  Object.defineProperty(navigator, "serviceWorker", { value: serviceWorker, configurable: true });
  return serviceWorker;
}

const requestPermission = vi.fn(async () => "granted");

beforeEach(() => {
  vi.stubGlobal("Notification", { requestPermission });
  vi.stubGlobal("PushManager", function PushManager() {});
});

afterEach(() => {
  Reflect.deleteProperty(navigator, "serviceWorker");
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  requestPermission.mockClear();
  window.localStorage.clear();
});

describe("app worker registration", () => {
  it("registers /sw.js without asking for notification permission", async () => {
    const sw = stubServiceWorker();
    registerAppWorker();
    expect(sw.register).toHaveBeenCalledWith("/sw.js");
    expect(requestPermission).not.toHaveBeenCalled();
  });

  it("does nothing where service workers are unavailable", () => {
    expect(() => registerAppWorker()).not.toThrow();
  });

  it("is registered when the app mounts", async () => {
    const sw = stubServiceWorker();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        const body: unknown = url.includes("/auth/config")
          ? { mode: "community", initialized: true }
          : [];
        return { ok: true, status: 200, json: async () => body, text: async () => "[]" };
      }),
    );
    const [{ render, screen }, { RouterProvider }, { getRouter }] = await Promise.all([
      import("@testing-library/react"),
      import("@tanstack/react-router"),
      import("../router"),
    ]);
    window.history.replaceState({}, "", "/login");
    const router = getRouter();
    await router.load();
    render(<RouterProvider router={router} />);
    await screen.findByRole("button", { name: "Sign In" });
    expect(sw.register).toHaveBeenCalledWith("/sw.js");
    expect(requestPermission).not.toHaveBeenCalled();
  });

  it("leaves the push subscribe path on the same worker URL", async () => {
    const sw = stubServiceWorker();
    const { api } = await import("@/lib/api");
    vi.spyOn(api, "get").mockResolvedValue({ publicKey: "AQAB" });
    vi.spyOn(api, "post").mockResolvedValue(undefined);
    const { subscribeToPush } = await import("@/lib/push-notifications");
    await subscribeToPush();
    expect(requestPermission).toHaveBeenCalled();
    expect(sw.register).toHaveBeenCalledWith("/sw.js");
  });
});

describe("signing out purges the app cache", () => {
  it("posts a purge to the worker on dev sign-out", () => {
    const sw = stubServiceWorker();
    window.localStorage.setItem("oc8-dev-token", "t");
    logoutDev();
    expect(sw.controller.postMessage).toHaveBeenCalledWith({ type: "purge" });
  });

  it("posts a purge to the worker on community sign-out", async () => {
    const sw = stubServiceWorker();
    window.localStorage.setItem("oc8-community-token", "t");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, json: async () => ({}) })),
    );
    const reload = vi.fn();
    vi.spyOn(window, "location", "get").mockReturnValue({
      ...window.location,
      reload,
    } as Location);
    await logoutCommunity();
    expect(sw.controller.postMessage).toHaveBeenCalledWith({ type: "purge" });
    expect(reload).toHaveBeenCalled();
  });
});
