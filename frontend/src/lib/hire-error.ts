import { ApiError } from "@/lib/api";

/** The machine code of a refused request. FastAPI wraps a dict `detail` as
 *  `{detail: {error}}`; a handler such as `credential_incomplete` uses
 *  `{detail: {code}}`. `toError` in `lib/api.ts` keeps that body on the error. */
export function errorCode(err: unknown): string | null {
  if (!(err instanceof ApiError)) return null;
  const body = err.body as { detail?: unknown; error?: unknown } | undefined;
  const detail = body?.detail;
  if (detail !== null && typeof detail === "object") {
    const d = detail as { error?: unknown; code?: unknown };
    if (typeof d.error === "string") return d.error;
    if (typeof d.code === "string") return d.code;
  }
  return typeof body?.error === "string" ? body.error : null;
}
