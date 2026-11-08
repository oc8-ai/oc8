// Which governed UI components (charts, tables, cards -- run-record-card.tsx's
// RUN_COMPONENT_REGISTRY) an agent or department may render via
// render_component. Before this existed there was no way to create a
// ComponentGrant row at all, so `render_component` always refused --
// see the "table component isn't unlocked for me" report that surfaced it.
//
// The catalogue is a short, fixed list (4 keys today), not a tenant-created
// resource -- so unlike knowledge-base assignment this is a toggle
// (grant/revoke), not a one-directional "assign" picker.

import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import {
  useComponentGrants,
  useCreateComponentGrant,
  useDeleteComponentGrant,
  type ComponentGrantDTO,
} from "@/lib/hooks";
import { useT } from "@/lib/i18n";

const CATALOG: { key: string; label: string; labelDe: string }[] = [
  { key: "record_card", label: "Record card", labelDe: "Datensatz-Karte" },
  { key: "data_table", label: "Data table", labelDe: "Tabelle" },
  { key: "bar_chart", label: "Bar chart", labelDe: "Balkendiagramm" },
  { key: "line_chart", label: "Line chart", labelDe: "Liniendiagramm" },
];

export function ComponentGrantPanel({
  granteeType,
  granteeId,
  mayManage,
  departmentId,
}: {
  granteeType: "agent" | "department";
  granteeId: string;
  mayManage: boolean;
  // This agent's department -- its grants are shown as "inherited" and
  // locked, same convention as KnowledgeAssignment's departmentEnabled.
  // Only meaningful when granteeType === "agent"; omit for a department panel.
  departmentId?: string | null;
}) {
  const t = useT();
  const { data: grants } = useComponentGrants();
  const createGrant = useCreateComponentGrant();
  const deleteGrant = useDeleteComponentGrant();

  const ownGrants = (grants ?? []).filter(
    (g: ComponentGrantDTO) => g.granteeType === granteeType && g.granteeId === granteeId,
  );
  const ownKeys = new Set(ownGrants.map((g) => g.componentKey));
  const inheritedKeys = departmentId
    ? (grants ?? [])
        .filter((g) => g.granteeType === "department" && g.granteeId === departmentId)
        .map((g) => g.componentKey)
    : [];
  const busy = createGrant.isPending || deleteGrant.isPending;

  function toggle(key: string, label: string) {
    const existing = ownGrants.find((g) => g.componentKey === key);
    if (existing) {
      deleteGrant.mutate(existing.id, {
        onError: () =>
          toast.error(t("Couldn't revoke access", "Zugriff konnte nicht entzogen werden"), {
            description: label,
          }),
      });
    } else {
      createGrant.mutate(
        { componentKey: key, granteeType, granteeId },
        {
          onError: () =>
            toast.error(t("Couldn't grant access", "Zugriff konnte nicht erteilt werden"), {
              description: label,
            }),
        },
      );
    }
  }

  return (
    <Panel className="p-5">
      <div className="micro-label">{t("visual output", "visuelle Ausgabe")}</div>
      <h3 className="mt-1 font-serif text-lg">{t("Components", "Komponenten")}</h3>
      <p className="mt-1 text-xs text-muted-foreground">
        {t(
          "Which visual components (charts, tables, record cards) may be used when responding.",
          "Welche visuellen Komponenten (Diagramme, Tabellen, Datensatz-Karten) bei Antworten verwendet werden dürfen.",
        )}
      </p>
      <div className="mt-4 space-y-2">
        {CATALOG.map((c) => {
          const label = t(c.label, c.labelDe);
          const granted = ownKeys.has(c.key);
          const inherited = !granted && inheritedKeys.includes(c.key);
          return (
            <label
              key={c.key}
              className="flex items-center justify-between rounded-md border border-border bg-background/40 px-3 py-2 text-sm"
            >
              <div>
                <div className="font-medium">{label}</div>
                {inherited && (
                  <div className="text-xs text-muted-foreground">
                    {t("Inherited from department", "Von der Abteilung geerbt")}
                  </div>
                )}
              </div>
              <input
                type="checkbox"
                checked={granted || inherited}
                disabled={!mayManage || inherited || busy}
                onChange={() => toggle(c.key, label)}
                className="h-4 w-4 accent-primary disabled:cursor-not-allowed"
              />
            </label>
          );
        })}
      </div>
    </Panel>
  );
}
