// Where a person was when their session ended, so signing in again takes them
// back there instead of to the dashboard. Kept in sessionStorage: it survives
// the reload and an identity provider's redirect in the same tab, and no other.

const KEY = "oc8-return-to";

function isOwnPage(path: string): boolean {
  return path.startsWith("/") && !path.startsWith("//") && !path.startsWith("/login");
}

export function rememberReturnPath(pathname: string, search: string): void {
  if (!isOwnPage(pathname)) return;
  try {
    window.sessionStorage.setItem(KEY, `${pathname}${search}`);
  } catch {
    // Storage blocked: the sign-in just goes home.
  }
}

/** The remembered page, cleared as it is read; "/" when there is none. */
export function takeReturnPath(): string {
  try {
    const path = window.sessionStorage.getItem(KEY);
    window.sessionStorage.removeItem(KEY);
    return path && isOwnPage(path) ? path : "/";
  } catch {
    return "/";
  }
}
