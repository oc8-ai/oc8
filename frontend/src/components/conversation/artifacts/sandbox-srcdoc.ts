// The document a sandboxed preview frame shows (spec §4.1, §9): the
// untrusted page inside one whose first head child is a strict CSP meta.
//
// Isolation rests on the frame (`sandbox="allow-scripts"`, no
// `allow-same-origin`: an opaque origin with no cookies, storage or API
// access, see SandboxFrame); the CSP adds no network at all. The page is put
// after our <head>, so whatever it starts with (its own doctype, <html>,
// <head>, a CSP meta of its own) can come no earlier than ours -- the parser
// merges a second <html>/<body> into ours, and a CSP meta outside <head> is
// not read (one inside could only tighten the policy anyway).

/** For a plain HTML page: inline script and style, images, fonts and media
 *  from data:/blob: only; no fetch/XHR/WebSocket, no frames, no forms, no
 *  <base>. */
export const SANDBOX_CSP =
  "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; " +
  "img-src data: blob:; font-src data:; media-src data: blob:; " +
  "base-uri 'none'; form-action 'none'";

/** Directives `extraCsp` cannot set: the frame never gets network, frames,
 *  form targets or a base URL, whatever a caller asks for. */
const FIXED = new Set(["connect-src", "frame-src", "child-src", "form-action", "base-uri"]);

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

/** The srcdoc for a sandboxed frame showing `body` (a page or a fragment).
 *  `extraCsp` widens the policy for a frame that needs it (Run Python:
 *  wasm, a blob: worker), see `sandboxCsp`. */
export function buildSandboxSrcdoc(body: string, extraCsp?: string): string {
  return (
    "<!doctype html><html><head>" +
    `<meta http-equiv="Content-Security-Policy" content="${attribute(sandboxCsp(extraCsp))}">` +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    `</head><body>${body}</body></html>`
  );
}
