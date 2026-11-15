// What an Enable click may grant: every permission a capa asks for except the
// ones it defers. A deferred permission (e.g. `knowledge:write`) waits for its
// own consent step (`POST /capas/{id}/grant`); passing it to
// `/capas/{id}/enable` would grant it silently.
export function enablePermissions(capa: {
  permissions?: string[] | null;
  deferredPermissions?: string[] | null;
}): string[] {
  const deferred = new Set(capa.deferredPermissions ?? []);
  return (capa.permissions ?? []).filter((permission) => !deferred.has(permission));
}
