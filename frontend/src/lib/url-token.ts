// One-time tokens from a mailed link (confirm email, reset password). The
// backend mints them with `secrets.token_urlsafe(32)`: 43 URL-safe base64
// characters. Only that shape is taken (with some room either side), and the
// token leaves the address bar as soon as it is read, so the secret does not
// stay in the history or show on a shared screen.
import { useEffect } from "react";

const TOKEN_RE = /^[A-Za-z0-9_-]{32,128}$/;

/** The token when it is well formed, otherwise "". */
export function urlToken(value: unknown): string {
  return typeof value === "string" && TOKEN_RE.test(value) ? value : "";
}

// The page is mounted again after the sign-in gate settles, by which time
// the address bar no longer carries the token: what was read is kept here,
// per history entry (cleaning the URL keeps the entry and its key), so a
// later visit to the same page never sees an earlier link's token.
const held = new Map<string, string>();

function entryKey(): string {
  if (typeof window === "undefined") return "";
  const state = window.history.state as { __TSR_key?: string; key?: string } | null;
  return `${window.location.pathname}#${state?.__TSR_key ?? state?.key ?? ""}`;
}

/** This page's token (from the URL, or as read before the URL was cleaned),
 *  and the query dropped from the address bar. */
export function useUrlToken(fromUrl: string): string {
  const entry = entryKey();
  if (fromUrl) held.set(entry, fromUrl);
  const token = fromUrl || held.get(entry) || "";
  useEffect(() => {
    if (!window.location.search) return;
    window.history.replaceState(
      window.history.state,
      "",
      window.location.pathname + window.location.hash,
    );
  }, []);
  return token;
}

const once = new Map<string, Promise<unknown>>();

/** A one-time token is spent by its first use: a page mounted twice must not
 *  send it twice (the second would fail as "already used"). */
export function spendOnce<T>(key: string, send: () => Promise<T>): Promise<T> {
  let pending = once.get(key) as Promise<T> | undefined;
  if (!pending) {
    pending = send();
    once.set(key, pending);
  }
  return pending;
}
