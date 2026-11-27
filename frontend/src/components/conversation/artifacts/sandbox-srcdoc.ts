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
// - GUARD (sandbox-guard.ts), the first script: a hardening layer in the
//   page's own realm. It takes away WebRTC (no CSP covers it in Chromium or
//   Firefox) and the other ways to send, and keeps any nested frame (the way
//   to a fresh realm without GUARD), resource hint or late refresh out of
//   the page;
// - `withoutNetworkHints`, in this (trusted) page before the srcdoc is
//   built: drops frames, refresh, <base>, resource hints, declarative shadow
//   roots, <noscript> and comments from the page.
// Static mode (`scripts: false`) has no scripts at all: `sandbox=""` and
// `script-src 'none'` -- for restricted answers and when an admin turned
// scripts in previews off.
//
// The page is put after our <head>, so whatever it starts with (its own
// doctype, <html>, <head>, a CSP meta of its own) comes after ours -- the
// parser merges a second <html>/<body> into ours, and a CSP meta outside
// <head> is not read (one inside could only tighten the policy anyway).

import { GUARD } from "./sandbox-guard";

export { GUARD };

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

/** Elements that would hold a nested document (a fresh realm without GUARD). */
const FRAMES = "iframe, frame, frameset, object, embed, portal, fencedframe";

/** The page without what could open a fresh realm, navigate it, rebase it or
 *  reach a host outside the CSP: frames, `<meta http-equiv=refresh>` (and a
 *  DNS prefetch switch of its own), `<base>`, resource-hint links, and
 *  declarative shadow roots (they would hide a frame from GUARD). Also
 *  `<noscript>` and comments: parsed here with scripting off, there with it
 *  on, they are where the two parsers would read the page differently.
 *  Parsed, not matched, so attribute order and quoting do not matter; the
 *  scripts are inert while parsed. Where there is no DOMParser (server
 *  render) the page stays as it is -- the frame is only ever shown in a
 *  browser, and GUARD and the CSPs do not depend on this. */
export function withoutNetworkHints(html: string): string {
  if (typeof DOMParser === "undefined") return html;
  const doc = new DOMParser().parseFromString(html, "text/html");
  for (const element of doc.querySelectorAll(`${FRAMES}, base, noscript`)) element.remove();
  for (const meta of doc.querySelectorAll("meta[http-equiv]")) {
    if (/refresh|dns-prefetch/i.test(meta.getAttribute("http-equiv") ?? "")) meta.remove();
  }
  for (const link of doc.querySelectorAll("link[rel]")) {
    const rels = (link.getAttribute("rel") ?? "").toLowerCase().split(/\s+/);
    if (rels.some((rel) => HINTS.has(rel))) link.remove();
  }
  for (const template of doc.querySelectorAll("template")) {
    template.removeAttribute("shadowrootmode");
    template.removeAttribute("shadowroot");
  }
  const comments = doc.createTreeWalker(doc, NodeFilter.SHOW_COMMENT);
  const found: Node[] = [];
  while (comments.nextNode()) found.push(comments.currentNode);
  for (const comment of found) comment.parentNode?.removeChild(comment);
  return doc.documentElement.outerHTML;
}

/** The srcdoc for a sandboxed frame showing `body` (a page or a fragment).
 *  `extraCsp` widens the policy for a frame that needs it (Run Python:
 *  wasm, a blob: worker), see `sandboxCsp`; GUARD runs in every frame with
 *  scripts. With `scripts: false` the frame runs none (`script-src 'none'`,
 *  and SandboxFrame drops `allow-scripts`). */
export function buildSandboxSrcdoc(
  body: string,
  extraCsp?: string,
  { scripts = true }: { scripts?: boolean } = {},
): string {
  const csp = scripts ? sandboxCsp(extraCsp) : sandboxCsp(`${extraCsp ?? ""}; script-src 'none'`);
  return (
    "<!doctype html><html><head>" +
    `<meta http-equiv="Content-Security-Policy" content="${attribute(csp)}">` +
    '<meta http-equiv="x-dns-prefetch-control" content="off">' +
    (scripts ? `<script>${GUARD}</script>` : "") +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    `</head><body>${withoutNetworkHints(body)}</body></html>`
  );
}
