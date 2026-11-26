// An approval threshold (`approval_eur`) holds a call at or above an amount,
// and the amount comes from the connection's manifest (its value spec). On a
// connection without one there is no amount, which counts as zero: €0 asks
// for every change and is kept, while a positive threshold never fires and the
// department's applies through the frame anyway (`connections.value_spec`).
// So a payload leaves a positive one out rather than echo a copy that went
// stale the moment the department changed its value (E2E F3 review).

type WithValueSpec = { name: string; hasValueSpec: boolean };

/** Whether each connection name declares a value spec. A name with no row
 *  yet (connections still loading) is absent: unknown, never "without". */
export function valueSpecByName(connections: readonly WithValueSpec[]): Map<string, boolean> {
  const byName = new Map<string, boolean>();
  for (const c of connections) byName.set(c.name, (byName.get(c.name) ?? false) || c.hasValueSpec);
  return byName;
}

/** The `approval_eur` field of a narrowing payload: a positive one is left out
 *  for a connection known to have no value spec; €0 and everything else is
 *  sent as given. */
export function thresholdField(
  name: string,
  approvalEur: number | null,
  specs: ReadonlyMap<string, boolean>,
): { approval_eur?: number | null } {
  if (specs.get(name) === false && approvalEur !== 0) return {};
  return { approval_eur: approvalEur };
}
