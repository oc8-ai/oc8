import { useMemo, useState } from "react";
import { Check, Loader2, Search } from "lucide-react";
import { toast } from "sonner";
import { StepDots } from "@/components/onboarding/step-dots";
import { Checkbox } from "@/components/ui/checkbox";
import { downloadCapaExport, type CapaExportItemInput } from "@/lib/api";
import { useAgents, useDepartments, usePreviewCapaExport, useSkills } from "@/lib/hooks";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

type WizardStep = "selection" | "details" | "preview" | "export";

const STEP_ORDER: WizardStep[] = ["selection", "details", "preview", "export"];
const STEP_LABEL: Record<WizardStep, [string, string]> = {
  selection: ["Selection", "Auswahl"],
  details: ["Details", "Details"],
  preview: ["Preview", "Vorschau"],
  export: ["Export", "Export"],
};

type Selection = {
  departmentIds: Set<string>;
  agentIds: Set<string>;
  skillIds: Set<string>;
  // At most one, and only ever set from `initialSelection` below -- unlike
  // the three id sets above, there's no picker UI for this: the Capas route
  // hands off a single just-installed tool-pack capa, pre-checked, never a
  // browsable list of tool packs to choose among.
  toolPack: { id: string; name: string } | null;
};

type ItemDetails = { name: string; version: string; summary: string };
type DetailsByKey = Record<string, ItemDetails>;

/** A capa name must match `_NAME_RE` in `backend/src/oc8/capas/export.py`
 * (lowercase, starts with a letter, only letters/digits/underscore). This is
 * only a friendly default for the Details step's input -- the backend is the
 * one enforcing the rule, and a bad name surfaces there as a Preview error. */
function slugify(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .replace(/^([0-9])/, "a$1");
}

function detailKey(kind: CapaExportItemInput["kind"], id: string): string {
  return `${kind}:${id}`;
}

export function CapaExportWizard({
  onClose,
  // Set by the Capas route right after the custom MCP wizard installs a
  // capa, so "Save & export as capa" hands off straight into this wizard
  // with that one tool-pack capa already checked -- see `build_tool_pack_export`
  // (backend/src/oc8/capas/export.py) for why a tool-pack export has no
  // Details-step-editable name/version/summary of its own (it re-renders the
  // capa's OWN stored manifest, ignoring whatever this wizard would send).
  initialSelection,
}: {
  onClose: () => void;
  initialSelection?: { kind: "tool_pack"; id: string; name: string };
}) {
  const t = useT();
  const [step, setStep] = useState<WizardStep>("selection");

  // The Selection step renders every row as a checkbox, not a paginated list
  // view -- so each list hook is called with an explicit large pageSize
  // rather than the default (20) `ListQueryParams` would otherwise use.
  // 200, not 500: /departments, /agents, and /skills all cap their `limit`
  // query param at 200 server-side (Query(..., le=200)) -- 500 always fails
  // with 422 before the handler runs, silently emptying every section of
  // this step for every tenant (found live, 2026-09-02).
  const { data: departmentsPage } = useDepartments({ pageSize: 200 });
  const { data: agentsPage } = useAgents({ pageSize: 200 });
  const { data: skillsPage } = useSkills({ pageSize: 200 });
  const departments = departmentsPage?.items ?? [];
  const agents = agentsPage?.items ?? [];
  // Only locally authored skills are exportable (build_skill_export rejects
  // origin="store" -- capas/export.py) -- store-origin skills are simply
  // absent from this picker, not shown disabled.
  const localSkills = (skillsPage?.items ?? []).filter((s) => s.origin === "local");

  const [selection, setSelection] = useState<Selection>({
    departmentIds: new Set(),
    agentIds: new Set(),
    skillIds: new Set(),
    toolPack: initialSelection ? { id: initialSelection.id, name: initialSelection.name } : null,
  });
  const [details, setDetails] = useState<DetailsByKey>({});
  const preview = usePreviewCapaExport();
  const [exporting, setExporting] = useState(false);

  // An agent that belongs to a selected department is already carried by
  // that department's own export (build_department_export walks every agent
  // in the department) -- offering it again as a standalone pick would
  // double-export it under a second capa name.
  const coveredAgentIds = new Set(
    agents
      .filter((a) => a.departmentId && selection.departmentIds.has(a.departmentId))
      .map((a) => a.id),
  );
  const selectableAgents = agents.filter((a) => !coveredAgentIds.has(a.id));

  const hasSelection =
    selection.departmentIds.size > 0 ||
    selection.agentIds.size > 0 ||
    selection.skillIds.size > 0 ||
    selection.toolPack !== null;

  function toggle(kind: keyof Selection, id: string, checked: boolean) {
    setSelection((prev) => {
      const next = new Set(prev[kind]);
      if (checked) next.add(id);
      else next.delete(id);
      return { ...prev, [kind]: next };
    });
  }

  function nameFor(kind: CapaExportItemInput["kind"], id: string): string {
    if (kind === "department") return departments.find((d) => d.id === id)?.name ?? "";
    if (kind === "agent") return agents.find((a) => a.id === id)?.name ?? "";
    return localSkills.find((s) => s.id === id)?.name ?? "";
  }

  function detailFor(kind: CapaExportItemInput["kind"], id: string): ItemDetails {
    return (
      details[detailKey(kind, id)] ?? {
        name: slugify(nameFor(kind, id)),
        version: "1.0.0",
        summary: "",
      }
    );
  }

  function setDetailField(
    kind: CapaExportItemInput["kind"],
    id: string,
    field: keyof ItemDetails,
    value: string,
  ) {
    setDetails((prev) => {
      const key = detailKey(kind, id);
      const current = prev[key] ?? detailFor(kind, id);
      return { ...prev, [key]: { ...current, [field]: value } };
    });
  }

  function buildItems(): CapaExportItemInput[] {
    const items: CapaExportItemInput[] = [];
    if (selection.toolPack) {
      // version/summary are the same request-shape defaults `detailFor`
      // falls back to for the other three kinds -- `build_tool_pack_export`
      // ignores all three (see the comment on `initialSelection` above), so
      // there's no Details-step row to source them from here.
      items.push({
        kind: "tool_pack",
        id: selection.toolPack.id,
        name: selection.toolPack.name,
        version: "1.0.0",
        summary: "",
      });
    }
    for (const deptId of selection.departmentIds) {
      const dept = departments.find((d) => d.id === deptId);
      if (!dept) continue;
      const d = detailFor("department", deptId);
      items.push({
        kind: "department",
        id: deptId,
        name: d.name,
        version: d.version,
        summary: d.summary,
      });
    }
    for (const agentId of selection.agentIds) {
      const agent = agents.find((a) => a.id === agentId);
      if (!agent) continue;
      const d = detailFor("agent", agentId);
      items.push({
        kind: "agent",
        id: agentId,
        name: d.name,
        version: d.version,
        summary: d.summary,
      });
    }
    for (const skillId of selection.skillIds) {
      const skill = localSkills.find((s) => s.id === skillId);
      if (!skill) continue;
      const d = detailFor("skill", skillId);
      items.push({
        kind: "skill",
        id: skillId,
        name: d.name,
        version: d.version,
        summary: d.summary,
      });
    }
    return items;
  }

  function goToPreview() {
    preview.mutate(buildItems());
    setStep("preview");
  }

  async function handleExport() {
    setExporting(true);
    try {
      const { blob, filename } = await downloadCapaExport(buildItems());
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
      toast.success(t("Capa exported", "Capa exportiert"), { description: filename });
      onClose();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : t("Export failed", "Export fehlgeschlagen"));
    } finally {
      setExporting(false);
    }
  }

  const stepIdx = STEP_ORDER.indexOf(step);

  return (
    <div className="space-y-4">
      <StepDots steps={STEP_ORDER} labels={STEP_LABEL} current={step} />

      <div className="max-h-[55vh] overflow-y-auto">
        {step === "selection" && (
          <SelectionStep
            departments={departments}
            selectableAgents={selectableAgents}
            localSkills={localSkills}
            selection={selection}
            onToggle={toggle}
          />
        )}

        {step === "details" && (
          <DetailsStep
            departments={departments}
            agents={agents}
            localSkills={localSkills}
            selection={selection}
            detailFor={detailFor}
            onChange={setDetailField}
          />
        )}

        {step === "preview" && <PreviewStep preview={preview} />}

        {step === "export" && (
          <p className="text-sm text-muted-foreground">
            {t(
              "Click Export to download the ZIP file.",
              "Klicke Export, um die ZIP-Datei herunterzuladen.",
            )}
          </p>
        )}
      </div>

      <div className="flex justify-between border-t border-border pt-3">
        <button
          type="button"
          onClick={() => (stepIdx === 0 ? onClose() : setStep(STEP_ORDER[stepIdx - 1]))}
          className="rounded-md px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground"
        >
          {stepIdx === 0 ? t("Cancel", "Abbrechen") : t("Back", "Zurück")}
        </button>

        {step === "selection" && (
          <button
            type="button"
            disabled={!hasSelection}
            onClick={() => setStep("details")}
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground disabled:opacity-50"
          >
            {t("Next", "Weiter")}
          </button>
        )}

        {step === "details" && (
          <button
            type="button"
            onClick={goToPreview}
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground"
          >
            {t("Preview", "Vorschau")}
          </button>
        )}

        {step === "preview" && (
          <button
            type="button"
            disabled={preview.isPending}
            onClick={() => setStep("export")}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground disabled:opacity-50"
          >
            {preview.isPending && <Loader2 className="h-3 w-3 animate-spin" />}
            {t("Continue to export", "Weiter zum Export")}
          </button>
        )}

        {step === "export" && (
          <button
            type="button"
            disabled={exporting || preview.isPending || !preview.isSuccess}
            onClick={handleExport}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground disabled:opacity-50"
          >
            {exporting && <Loader2 className="h-3 w-3 animate-spin" />}
            {exporting ? t("Exporting…", "Exportiere…") : t("Export", "Export")}
          </button>
        )}
      </div>
    </div>
  );
}

/** A selectable "row card" -- the same bordered/rounded treatment the
 * guided-setup steps and credential picker already use elsewhere on the
 * Settings page (`routes/settings.tsx`), rather than a bare native checkbox
 * + label. The whole row is the click target, not just the checkbox. */
function SelectableRow({
  id,
  label,
  checked,
  onCheckedChange,
}: {
  id: string;
  label: string;
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
}) {
  return (
    <label
      htmlFor={id}
      className={cn(
        "flex cursor-pointer items-center gap-2.5 rounded-md border px-3 py-2 text-sm transition",
        checked
          ? "border-primary bg-primary/10"
          : "border-border bg-background/30 hover:border-primary/40",
      )}
    >
      <Checkbox
        id={id}
        checked={checked}
        onCheckedChange={(value) => onCheckedChange(value === true)}
      />
      {label}
    </label>
  );
}

/** One section of the Selection step: a header with a live match count, a
 * fixed-height scroll container (so a tenant with 100+ agents or 200+
 * skills can't blow the wizard's own height past `max-h-[55vh]`), and two
 * distinct empty states -- nothing exists yet vs. nothing matches the
 * current search, since those call for different copy. */
function SelectionSection({
  title,
  items,
  query,
  emptyMessage,
  idPrefix,
  selectedIds,
  onCheckedChange,
}: {
  title: string;
  items: { id: string; name: string }[];
  query: string;
  emptyMessage: string;
  idPrefix: string;
  selectedIds: Set<string>;
  onCheckedChange: (id: string, checked: boolean) => void;
}) {
  const t = useT();
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((item) => item.name.toLowerCase().includes(q));
  }, [items, query]);

  return (
    <section>
      <p className="text-sm font-medium">
        {title}
        {items.length > 0 && (
          <span className="ml-1.5 font-normal text-muted-foreground">
            (
            {filtered.length === items.length ? items.length : `${filtered.length}/${items.length}`}
            )
          </span>
        )}
      </p>
      {items.length === 0 && <p className="mt-1 text-xs text-muted-foreground">{emptyMessage}</p>}
      {items.length > 0 && filtered.length === 0 && (
        <p className="mt-1 text-xs text-muted-foreground">
          {t(`No matches for "${query}".`, `Keine Treffer für „${query}".`)}
        </p>
      )}
      {filtered.length > 0 && (
        <div className="mt-2 max-h-56 space-y-1.5 overflow-y-auto pr-1">
          {filtered.map((item) => (
            <SelectableRow
              key={item.id}
              id={`${idPrefix}-${item.id}`}
              label={item.name}
              checked={selectedIds.has(item.id)}
              onCheckedChange={(checked) => onCheckedChange(item.id, checked)}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function SelectionStep({
  departments,
  selectableAgents,
  localSkills,
  selection,
  onToggle,
}: {
  departments: { id: string; name: string }[];
  selectableAgents: { id: string; name: string }[];
  localSkills: { id: string; name: string }[];
  selection: Selection;
  onToggle: (kind: keyof Selection, id: string, checked: boolean) => void;
}) {
  const t = useT();
  const [query, setQuery] = useState("");
  // 100+ agents / 200+ skills is a real tenant shape this wizard has to
  // stay usable for, not just a handful of test fixtures -- one search box
  // narrows all three sections at once instead of forcing a scroll hunt
  // through each.
  const totalItems = departments.length + selectableAgents.length + localSkills.length;
  return (
    <div className="space-y-5">
      {selection.toolPack && (
        <section>
          <p className="text-sm font-medium">{t("Custom MCP capa", "Custom-MCP-Capa")}</p>
          <div className="mt-2 flex items-center gap-2.5 rounded-md border border-primary bg-primary/10 px-3 py-2 text-sm">
            <Check className="h-4 w-4 shrink-0 text-primary" />
            {selection.toolPack.name}
          </div>
        </section>
      )}

      {totalItems > 0 && (
        <div className="relative">
          <Search className="pointer-events-none absolute left-2 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <input
            type="text"
            placeholder={t(
              "Search departments, agents, skills…",
              "Departments, Agenten, Skills durchsuchen…",
            )}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="w-full rounded-md border border-border bg-panel py-1.5 pl-8 pr-2 text-sm"
          />
        </div>
      )}

      <SelectionSection
        title={t("Departments", "Departments")}
        items={departments}
        query={query}
        emptyMessage={t("No departments yet.", "Noch keine Departments.")}
        idPrefix="export-department"
        selectedIds={selection.departmentIds}
        onCheckedChange={(id, checked) => onToggle("departmentIds", id, checked)}
      />

      <SelectionSection
        title={t("Standalone agents", "Einzelne Agenten")}
        items={selectableAgents}
        query={query}
        emptyMessage={t(
          "No agents outside a selected department.",
          "Keine Agenten außerhalb eines ausgewählten Departments.",
        )}
        idPrefix="export-agent"
        selectedIds={selection.agentIds}
        onCheckedChange={(id, checked) => onToggle("agentIds", id, checked)}
      />

      <SelectionSection
        title={t("Skills", "Skills")}
        items={localSkills}
        query={query}
        emptyMessage={t(
          "No locally authored skills to export.",
          "Keine lokal erstellten Skills zum Exportieren.",
        )}
        idPrefix="export-skill"
        selectedIds={selection.skillIds}
        onCheckedChange={(id, checked) => onToggle("skillIds", id, checked)}
      />
    </div>
  );
}

function DetailsStep({
  departments,
  agents,
  localSkills,
  selection,
  detailFor,
  onChange,
}: {
  departments: { id: string; name: string }[];
  agents: { id: string; name: string }[];
  localSkills: { id: string; name: string }[];
  selection: Selection;
  detailFor: (kind: CapaExportItemInput["kind"], id: string) => ItemDetails;
  onChange: (
    kind: CapaExportItemInput["kind"],
    id: string,
    field: keyof ItemDetails,
    value: string,
  ) => void;
}) {
  const t = useT();
  const rows: { kind: CapaExportItemInput["kind"]; id: string; label: string }[] = [
    ...[...selection.departmentIds].flatMap((id) => {
      const dept = departments.find((d) => d.id === id);
      return dept ? [{ kind: "department" as const, id, label: dept.name }] : [];
    }),
    ...[...selection.agentIds].flatMap((id) => {
      const agent = agents.find((a) => a.id === id);
      return agent ? [{ kind: "agent" as const, id, label: agent.name }] : [];
    }),
    ...[...selection.skillIds].flatMap((id) => {
      const skill = localSkills.find((s) => s.id === id);
      return skill ? [{ kind: "skill" as const, id, label: skill.name }] : [];
    }),
  ];

  return (
    <div className="space-y-4">
      {rows.map(({ kind, id, label }) => {
        const current = detailFor(kind, id);
        return (
          <div key={`${kind}:${id}`} className="space-y-2 rounded-md border border-border p-3">
            <p className="text-sm font-medium">{label}</p>
            <label className="block text-xs text-muted-foreground">
              {t("Capa name", "Capa-Name")}
              <input
                className="mt-1 w-full rounded border border-border bg-background/40 px-2 py-1 text-sm text-foreground"
                value={current.name}
                onChange={(e) => onChange(kind, id, "name", e.target.value)}
              />
            </label>
            <label className="block text-xs text-muted-foreground">
              {t("Version", "Version")}
              <input
                className="mt-1 w-full rounded border border-border bg-background/40 px-2 py-1 text-sm text-foreground"
                value={current.version}
                onChange={(e) => onChange(kind, id, "version", e.target.value)}
              />
            </label>
            <label className="block text-xs text-muted-foreground">
              {t("Summary", "Kurzbeschreibung")}
              <input
                className="mt-1 w-full rounded border border-border bg-background/40 px-2 py-1 text-sm text-foreground"
                value={current.summary}
                onChange={(e) => onChange(kind, id, "summary", e.target.value)}
              />
            </label>
          </div>
        );
      })}
    </div>
  );
}

function PreviewStep({ preview }: { preview: ReturnType<typeof usePreviewCapaExport> }) {
  const t = useT();
  if (preview.isPending) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        {t("Building preview…", "Vorschau wird erstellt…")}
      </div>
    );
  }
  if (preview.isError) {
    return (
      <p className="text-sm text-destructive">
        {preview.error instanceof Error ? preview.error.message : String(preview.error)}
      </p>
    );
  }
  return (
    <div className="space-y-3">
      {preview.data?.errors.map((e) => (
        <p key={e} className="text-sm text-destructive">
          {e}
        </p>
      ))}
      {preview.data?.items.map((item) => (
        <div key={item.folder_name} className="rounded-md border border-border p-3">
          <p className="text-sm font-medium">{item.folder_name}/</p>
          {item.warnings.map((w) => (
            <p key={w} className="text-xs text-amber-500">
              ⚠ {w}
            </p>
          ))}
          <pre className="mt-2 max-h-40 overflow-y-auto whitespace-pre-wrap break-all rounded bg-background/40 p-2 text-xs text-muted-foreground">
            {item.manifest_toml}
          </pre>
          {Object.entries(item.extra_files).map(([path, content]) => (
            <div key={path} className="mt-2">
              <p className="text-[11px] text-muted-foreground">{path}</p>
              <pre className="mt-1 max-h-40 overflow-y-auto whitespace-pre-wrap break-all rounded bg-background/40 p-2 text-xs text-muted-foreground">
                {content}
              </pre>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
