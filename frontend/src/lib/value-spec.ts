// An approval threshold (`approval_eur`) holds a call above an amount, so it
// needs the connection's manifest to say where a record's amount lives (its
// value spec). On a connection without one, an agent's own threshold could
// never hold, and the department's applies through the frame anyway: the
// backend drops such a value instead of storing it (`connections.value_spec`).
// So a payload leaves it out rather than echo a copy that went stale the moment
// the department changed its value (E2E F3 review).

type WithValueSpec = { name: string; hasValueSpec: boolean };

/** Whether each connection name declares a value spec. A name with no row
 *  yet (connections still loading) is absent: unknown, never "without". */
export function valueSpecByName(connections: readonly WithValueSpec[]): Map<string, boolean> {
  const byName = new Map<string, boolean>();
  for (const c of connections) byName.set(c.name, (byName.get(c.name) ?? false) || c.hasValueSpec);
  return byName;
}

/** The `approval_eur` field of a narrowing payload: left out for a connection
 *  known to have no value spec, sent as given otherwise. */
export function thresholdField(
  name: string,
  approvalEur: number | null,
  specs: ReadonlyMap<string, boolean>,
): { approval_eur?: number | null } {
  return specs.get(name) === false ? {} : { approval_eur: approvalEur };
}
