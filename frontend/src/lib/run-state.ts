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
