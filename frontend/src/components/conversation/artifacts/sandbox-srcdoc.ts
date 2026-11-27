// The document a sandboxed preview frame shows (spec §4.1, §9): the
// untrusted page inside one whose first head child is a strict CSP meta.
//
// Threat: the page can hold business data (it is the model's answer), and a
// model steered by injected text can write script that tries to send it
// out. The layers, outermost first:
// - the frame: `sandbox="allow-scripts"` only, so an opaque origin (no
//   cookies, storage, oc8 API, parent DOM, popups, forms, top navigation);
//   see SandboxFrame;
// - the app's own response CSP (server/frame-ancestors.ts): `frame-src
//   'none'`, so the frame cannot navigate itself to another URL (a link,
//   `location = …`, a refresh) -- the check runs before any request;
// - this CSP: no network for fetch/XHR/WebSocket, images, styles, fonts;
// - GUARD, the first script: takes away the network APIs CSP does not
//   cover in every browser (WebRTC has no CSP in Chromium) and removes any
//   nested frame before it can load (a nested srcdoc would run script in a
//   new window without GUARD);
// - `withoutNetworkHints`: drops refresh, <base>, resource hints (DNS
//   prefetch is not under CSP) and declarative shadow roots from the page.
//   Best effort: GUARD and the CSPs do not depend on it.
//
// The page is put after our <head>, so whatever it starts with (its own
// doctype, <html>, <head>, a CSP meta of its own) comes after ours -- the
// parser merges a second <html>/<body> into ours, and a CSP meta outside
// <head> is not read (one inside could only tighten the policy anyway).

/** For a plain HTML page: inline script and style, images, fonts and media
 *  from data:/blob: only; no fetch/XHR/WebSocket, no frames, no forms, no
 *  <base>, no WebRTC (where a browser knows the directive). */
export const SANDBOX_CSP =
  "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; " +
  "img-src data: blob:; font-src data:; media-src data: blob:; " +
  "base-uri 'none'; form-action 'none'; webrtc 'block'";

/** Directives `extraCsp` cannot set: the frame never gets network, frames,
 *  form targets, a base URL or WebRTC, whatever a caller asks for. */
const FIXED = new Set([
  "connect-src",
  "frame-src",
  "child-src",
  "form-action",
  "base-uri",
  "webrtc",
]);

/** Runs before anything of the page. Every network API that is not needed
 *  (the Python runner gets its bytes by postMessage, see §9) is replaced by
 *  an undefined, non-configurable, non-writable property, so the page can
 *  neither call nor restore it; `attachShadow` too, so no frame can hide in
 *  a shadow tree from the observer. Any <iframe>, <frame>, <object> or
 *  <embed> is removed in the microtask after it is inserted: a nested
 *  frame's document loads in a later task, so it never runs. (A nested
 *  about:blank window cannot be reached either: in a sandbox each document
 *  has its own opaque origin.) */
export const GUARD = `(() => {
  const gone = (target, name) => {
    try {
      Object.defineProperty(target, name, { value: undefined, writable: false, configurable: false });
    } catch {}
  };
  for (const name of ["RTCPeerConnection", "webkitRTCPeerConnection", "RTCDataChannel",
    "RTCIceTransport", "WebSocket", "WebTransport", "EventSource", "fetch", "XMLHttpRequest"]) {
    gone(window, name);
  }
  gone(Navigator.prototype, "sendBeacon");
  gone(navigator, "sendBeacon");
  gone(Element.prototype, "attachShadow");
  gone(Element.prototype, "setHTMLUnsafe");
  gone(ShadowRoot.prototype, "setHTMLUnsafe");
  gone(Document, "parseHTMLUnsafe");
  const FRAMES = "iframe,frame,object,embed";
  const drop = (node) => {
    if (node.nodeType !== 1) return;
    if (node.matches(FRAMES)) node.remove();
    else for (const frame of node.querySelectorAll(FRAMES)) frame.remove();
  };
  new MutationObserver((records) => {
    for (const record of records) for (const node of record.addedNodes) drop(node);
  }).observe(document, { childList: true, subtree: true });
})();`;

function directives(policy: string): [string, string][] {
  return policy
    .split(";")
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => {
      const [name, ...values] = part.split(/\s+/);
      return [name.toLowerCase(), values.join(" ")];
    });
}

/** SANDBOX_CSP with `extraCsp`'s directives put in place of the same-named
 *  ones (a policy reads only the first of a name, so appending would do
 *  nothing) or added; FIXED directives in `extraCsp` are dropped. */
export function sandboxCsp(extraCsp?: string): string {
  const merged = new Map(directives(SANDBOX_CSP));
  for (const [name, value] of directives(extraCsp ?? "")) {
    if (!FIXED.has(name)) merged.set(name, value);
  }
  return [...merged].map(([name, value]) => (value ? `${name} ${value}` : name)).join("; ");
}

function attribute(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

/** `<link rel>` values that reach a host without a fetch CSP checks. */
const HINTS = new Set([
  "dns-prefetch",
  "preconnect",
  "prefetch",
  "prerender",
  "preload",
  "modulepreload",
]);

/** The page without what could navigate it, rebase it or reach a host
 *  outside the CSP: `<meta http-equiv=refresh>`, `<base>`, resource-hint
 *  links, and declarative shadow roots (they would hide a frame from
 *  GUARD). Parsed, not matched, so attribute order and quoting do not
 *  matter; the scripts are inert while parsed. Where there is no
 *  DOMParser (server render) the page stays as it is -- the frame is only
 *  ever shown in a browser. */
export function withoutNetworkHints(html: string): string {
  if (typeof DOMParser === "undefined") return html;
  const doc = new DOMParser().parseFromString(html, "text/html");
  for (const meta of doc.querySelectorAll("meta[http-equiv]")) {
    if (/^\s*refresh\s*$/i.test(meta.getAttribute("http-equiv") ?? "")) meta.remove();
  }
  for (const base of doc.querySelectorAll("base")) base.remove();
  for (const link of doc.querySelectorAll("link[rel]")) {
    const rels = (link.getAttribute("rel") ?? "").toLowerCase().split(/\s+/);
    if (rels.some((rel) => HINTS.has(rel))) link.remove();
  }
  for (const template of doc.querySelectorAll("template")) {
    template.removeAttribute("shadowrootmode");
    template.removeAttribute("shadowroot");
  }
  return doc.documentElement.outerHTML;
}

/** The srcdoc for a sandboxed frame showing `body` (a page or a fragment).
 *  `extraCsp` widens the policy for a frame that needs it (Run Python:
 *  wasm, a blob: worker), see `sandboxCsp`; GUARD runs in every frame. */
export function buildSandboxSrcdoc(body: string, extraCsp?: string): string {
  return (
    "<!doctype html><html><head>" +
    `<meta http-equiv="Content-Security-Policy" content="${attribute(sandboxCsp(extraCsp))}">` +
    '<meta http-equiv="x-dns-prefetch-control" content="off">' +
    `<script>${GUARD}</script>` +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    `</head><body>${withoutNetworkHints(body)}</body></html>`
  );
}
