import type { EditionServerHooks } from "../edition/contracts";

const HOOK_TIMEOUT_MS = 2000;
const ORIGIN_PATTERN =
  /^(https:\/\/[a-z0-9.-]+(:\d{1,5})?|http:\/\/(localhost|127\.0\.0\.1)(:\d{1,5})?)$/;

export function isAllowedAncestor(origin: string): boolean {
  return ORIGIN_PATTERN.test(origin);
}

async function resolveOrigins(request: Request, hooks: EditionServerHooks): Promise<string[]> {
  if (!hooks.frameAncestors) return [];
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const timeout = new Promise<readonly string[]>((resolve) => {
      timer = setTimeout(() => resolve([]), HOOK_TIMEOUT_MS);
    });
    const origins = await Promise.race([hooks.frameAncestors(request), timeout]);
    return origins.filter(isAllowedAncestor);
  } catch (error) {
    console.error(error);
    return [];
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/**
 * What the app's pages may frame. The app frames nothing but sandboxed
 * artifact previews (srcdoc, which no `frame-src` blocks), so no frame may
 * load a URL: a preview that navigates itself to another page (to carry
 * data out in the URL) is stopped before the request is made. `child-src`
 * keeps workers working -- the push service worker ('self') and, inherited
 * by a preview, the Run Python worker (blob:).
 */
const NESTED_DIRECTIVES = ["frame-src 'none'", "child-src 'self' blob:"];
const NESTED_NAMES = /^(frame-ancestors|frame-src|child-src)(\s|$)/i;

/**
 * Refuses foreign framing by default (`frame-ancestors 'self'`); an edition can
 * allow further origins through its server hook. Invalid origins are dropped.
 * Also sets what the page itself may frame (see NESTED_DIRECTIVES).
 */
export async function withFrameAncestors(
  request: Request,
  response: Response,
  hooks: EditionServerHooks,
): Promise<Response> {
  const origins = await resolveOrigins(request, hooks);
  const directive = ["frame-ancestors 'self'", ...origins].join(" ");

  const headers = new Headers(response.headers);
  const existing = headers.get("content-security-policy");
  const kept = (existing ?? "")
    .split(";")
    .map((part) => part.trim())
    .filter((part) => part && !NESTED_NAMES.test(part));
  headers.set("content-security-policy", [...kept, directive, ...NESTED_DIRECTIVES].join("; "));

  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}
