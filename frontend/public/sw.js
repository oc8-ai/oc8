// frontend/public/sw.js
// Plain, unbundled JS -- Vite serves everything under public/ at the root
// as-is, so this file needs no build step and is fetchable at /sw.js. It is
// the one service worker for this origin (one worker per scope), registered
// at app start (src/lib/app-worker.ts) and reused by the push subscribe
// (src/lib/push-notifications.ts). It has two jobs:
//
// 1. Keep the app shell's static build files so the installed app starts
//    fast: cache-first for content-hashed files under /assets/ only. A hashed
//    file never changes under its name, so a cached copy is never stale.
//    Nothing else is cached -- no API, model or tool responses, no HTML, no
//    request carrying a token, and not /pyodide/* (left to the HTTP cache).
//    Signing out posts {type: "purge"}, which deletes every cache.
// 2. Push notifications (below).

// Bump to drop every older copy on the next activate.
const ASSET_CACHE = "oc8-assets-v1";
// Each deploy brings new hashed names; keep the newest this many files, which
// is above one build's working set, so the cache cannot grow without end.
const MAX_ASSETS = 200;

// <name>-<hash>.<ext> directly under /assets/; Vite's hash is 8 url-safe chars.
const HASHED_ASSET = /^\/assets\/[^/]+-[A-Za-z0-9_-]{8}\.[A-Za-z0-9]+$/;

function isCacheableRequest(request, origin) {
  if (request.method !== "GET" || request.mode === "navigate") return false;
  // A token-bearing request is never shared; a range answer is a slice (206).
  if (request.headers.has("authorization") || request.headers.has("range")) return false;
  // Encoded or dot-segment paths are never one of our hashed names, and the
  // server may decode them to something outside /assets/.
  if (request.url.includes("%") || request.url.includes("..")) return false;
  let url;
  try {
    url = new URL(request.url);
  } catch {
    return false;
  }
  if (url.origin !== origin || url.search) return false;
  return HASHED_ASSET.test(url.pathname);
}

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(names.filter((n) => n !== ASSET_CACHE).map((n) => caches.delete(n)));
      await self.clients.claim();
    })(),
  );
});

// Stores a copy and trims the oldest entries. Off the response path: a full
// or restricted Cache Storage must never fail a file the network served.
async function storeAsset(cache, request, response) {
  try {
    await cache.put(request, response);
    const keys = await cache.keys();
    const excess = keys.length - MAX_ASSETS;
    if (excess > 0) await Promise.all(keys.slice(0, excess).map((key) => cache.delete(key)));
  } catch {
    // Not cached this time; the next load fetches it again.
  }
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (!isCacheableRequest(request, self.location.origin)) return;
  event.respondWith(
    (async () => {
      let cache = null;
      try {
        cache = await caches.open(ASSET_CACHE);
        const hit = await cache.match(request);
        if (hit) return hit;
      } catch {
        // Cache Storage unavailable (quota, private mode): plain network.
        cache = null;
      }
      // A network failure stays a failure -- no made-up response.
      const response = await fetch(request);
      if (cache && response.status === 200 && response.type === "basic") {
        event.waitUntil(storeAsset(cache, request, response.clone()));
      }
      return response;
    })(),
  );
});

self.addEventListener("message", (event) => {
  if (!event.data || event.data.type !== "purge") return;
  event.waitUntil(caches.keys().then((names) => Promise.all(names.map((n) => caches.delete(n)))));
});

// A browser may rotate a push subscription on its own -- key rotation, storage
// pressure -- with nothing on the server having asked for it. Without this
// handler the stored endpoint goes stale, the next send gets a 410, the row is
// pruned, and nothing ever re-registers: the toggle still reads "on" while push
// is permanently dead.
//
// This worker cannot re-register the new subscription itself: this app
// authenticates with a bearer token kept in `localStorage`, which a service
// worker has no access to (no DOM, no `window`), and this origin's API has no
// cookie session for a plain `fetch(..., {credentials: "include"})` to ride on
// either. So instead of failing silently against an endpoint it can never
// authenticate to, this relays the event to any open tab, which already has
// the token and can redo the full authenticated subscribe via
// `subscribeToPush()` (see src/lib/push-notifications.ts, wired up in
// app-shell.tsx). If no tab is open when the browser rotates the key, nothing
// re-registers until the operator next opens one -- an accepted limitation,
// not a masked failure.
self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clientsList) => {
      for (const client of clientsList) {
        client.postMessage({ type: "oc8-push-subscription-changed" });
      }
    }),
  );
});

self.addEventListener("push", (event) => {
  if (!event.data) return;
  const payload = event.data.json();
  event.waitUntil(
    (async () => {
      const clientsList = await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });
      const hasFocusedClient = clientsList.some((client) => client.focused);
      if (hasFocusedClient) {
        // That operator is already looking at a tab and will see the
        // in-app toast (frontend/src/lib/live/toast-for-event.ts) -- a
        // native notification too would double-fire the same event.
        return;
      }
      await self.registration.showNotification(payload.title, {
        body: payload.body,
        data: { url: payload.url },
      });
    })(),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = event.notification.data && event.notification.data.url;
  if (!url) return;
  event.waitUntil(
    (async () => {
      const clientsList = await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });
      const target = clientsList.find((client) => {
        try {
          return new URL(client.url).origin === self.location.origin;
        } catch {
          return false;
        }
      });
      if (target) {
        await target.navigate(url);
        await target.focus();
      } else {
        await self.clients.openWindow(url);
      }
    })(),
  );
});
