// Ids read from the address bar (a path segment or a search param) go into
// API paths and navigation targets. Only a well-formed UUID is taken, so a
// crafted link ("?run=../../members/x") cannot make the browser call another
// endpoint.

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID_RE.test(value);
}

/** The id when it is a UUID, otherwise nothing. */
export function urlId(value: unknown): string | undefined {
  return isUuid(value) ? value : undefined;
}
