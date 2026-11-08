/** The one state -> colour mapping: the header, the Live Log and the run
 *  history all take their colour from here. Green is "done", the header's
 *  running colour is "running", amber is "waiting", red is "failed", and the
 *  neutral grey is for states that are neither good nor bad news. */
export const RUN_STATE_COLOR: Record<string, string> = {
  queued: "var(--status-paused)",
  running: "var(--status-running)",
  waiting_for_input: "var(--status-warning)",
  waiting_for_approval: "var(--status-warning)",
  done: "var(--status-waiting-for-task)",
  failed: "var(--status-error)",
  interrupted: "var(--status-paused)",
};

/** The one state -> words mapping, for a responsibility's states and a run's.
 *  An unknown state shows as itself rather than as nothing. */
export function stateLabel(t: (en: string, de?: string) => string, state: string): string {
  switch (state) {
    case "active":
      return t("active", "aktiv");
    case "waiting":
      return t("waiting", "wartet");
    case "paused":
      return t("paused", "pausiert");
    case "done":
      return t("done", "erledigt");
    case "cancelled":
      return t("cancelled", "abgebrochen");
    case "queued":
      return t("queued", "in der Warteschlange");
    case "running":
      return t("running", "läuft");
    case "waiting_for_input":
      return t("waiting for input", "wartet auf Eingabe");
    case "waiting_for_approval":
      return t("waiting for approval", "wartet auf Freigabe");
    case "failed":
      return t("failed", "fehlgeschlagen");
    case "interrupted":
      return t("interrupted", "unterbrochen");
    default:
      return state;
  }
}
