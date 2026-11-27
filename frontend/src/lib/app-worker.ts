// The one service worker (public/sw.js): registered at app start so the
// installed app's build files are cached without anyone granting notification
// permission first. The push subscribe registers the same URL, which returns
// this same registration.

/** Registers /sw.js. Best-effort: the app works the same without it. */
export function registerAppWorker(): void {
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return;
  void navigator.serviceWorker.register("/sw.js").catch(() => {});
}

/** Deletes the worker's caches -- part of signing out. The worker does it
 * (its `waitUntil` outlives the reload that follows a sign-out); a page the
 * worker does not control yet deletes them itself. */
export function purgeAppCaches(): void {
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return;
  const controller = navigator.serviceWorker.controller;
  if (controller) {
    controller.postMessage({ type: "purge" });
  } else if (typeof caches !== "undefined") {
    void caches
      .keys()
      .then((names) => Promise.all(names.map((name) => caches.delete(name))))
      .catch(() => {});
  }
}
