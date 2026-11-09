/** The department page's tabs; a link may open one with `?tab=` (e.g. a run's
 *  "open guardrails" link), read once on arrival. */
export const DEPARTMENT_TABS = ["overview", "guardrails", "collab", "settings"] as const;
export type DepartmentTab = (typeof DEPARTMENT_TABS)[number];

export function initialDepartmentTab(): DepartmentTab {
  const wanted = new URLSearchParams(window.location.search).get("tab");
  return (DEPARTMENT_TABS as readonly string[]).includes(wanted ?? "")
    ? (wanted as DepartmentTab)
    : "overview";
}
