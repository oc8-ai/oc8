import { createFileRoute } from "@tanstack/react-router";
import {
  Blocks,
  BookOpen,
  Check,
  Download,
  Plus,
  Search,
  ShieldAlert,
  Sparkles,
  Users2,
  Wrench,
  X,
} from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { DetailSheet } from "@/components/detail-sheet";
import { ListToolbar, groupItems, type ListQueryState } from "@/components/list-toolbar";
import {
  type CreateSkillBody,
  type ImportCandidate,
  useCreateSkill,
  useImportSkills,
  usePreviewSkillImport,
  useAgents,
  useAssignSkill,
  useDepartments,
  useRestoreSkill,
  useSkills,
  useUpdateSkill,
} from "@/lib/hooks";

type SkillDraft = CreateSkillBody;
import { skillCategories, type Skill, type SkillCategory } from "@/lib/skills";
import { cn } from "@/lib/utils";
import { useLang, useT } from "@/lib/i18n";

export const Route = createFileRoute("/skills")({
  component: SkillsPage,
});

export function SkillsPage() {
  const t = useT();
  const { lang } = useLang();
  const catLabel = (id: string) => {
    const c = skillCategories.find((x) => x.id === id);
    if (!c) return id;
    return lang === "de" ? c.labelDe : c.label;
  };

  const [queryState, setQueryState] = useState<ListQueryState>({
    search: "",
    filters: {},
    groupBy: null,
    includeArchived: false,
    page: 1,
    pageSize: 20,
  });
  const { data } = useSkills({
    search: queryState.search,
    filters: queryState.filters,
    groupBy: queryState.groupBy,
    includeArchived: queryState.includeArchived,
    page: queryState.page,
    pageSize: queryState.pageSize,
  });
  // The server is the source of truth: a created skill lands in the library
  // because the mutation invalidates the query, not because we stash a draft.
  const items = data?.items ?? [];
  const [selected, setSelected] = useState<Skill | null>(null);
  // Re-read the open skill off the freshly-fetched page instead of trusting the
  // snapshot taken when the card was clicked. Saving an edit invalidates the
  // list query, but `selected` would still hold the pre-edit row, so the sheet
  // header kept showing the OLD name next to the input holding the new one
  // (Task 22 live-E2E finding). Falls back to the snapshot when the edit moves
  // the row off the current page/filter, so the sheet never blanks out.
  const selectedLive = selected ? (items.find((s) => s.id === selected.id) ?? selected) : null;
  const [wizardOpen, setWizardOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);

  // Grouped by category/author (server-paginated -- only the current page's
  // items are grouped, matching what <ListToolbar>'s pagination controls show).
  const grouped = useMemo(
    () =>
      groupItems(items, queryState.groupBy, (s) =>
        queryState.groupBy === "category" ? catLabel(s.category) : (s.author ?? "—"),
      ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [items, queryState.groupBy, lang],
  );

  // The author filter's options come from whatever page is currently loaded
  // rather than a full-tenant fetch -- a `planning-time choice` (Task 16
  // brief) that keeps this to one query, at the cost of the dropdown only
  // offering authors visible on the current page/filter combination.
  const authorOptions = useMemo(() => {
    const seen = new Set<string>();
    const options: Array<{ value: string; label: string }> = [];
    for (const s of items) {
      if (s.author && !seen.has(s.author)) {
        seen.add(s.author);
        options.push({ value: s.author, label: s.author });
      }
    }
    return options;
  }, [items]);

  // "Total skills" is the real tenant-wide count (the server tells us via
  // totalCount); the other three are scoped to the current page only -- a
  // full-tenant aggregate would need a separate endpoint, out of scope here.
  const stats = useMemo(
    () => ({
      total: data?.totalCount ?? 0,
      local: items.filter((s) => s.origin === "local").length,
      store: items.filter((s) => s.origin === "store").length,
      assigned: items.reduce((n, s) => n + s.usedByAgents, 0),
    }),
    [items, data?.totalCount],
  );

  const createSkill = useCreateSkill();
  const restoreSkill = useRestoreSkill();

  const handleCreate = async (draft: SkillDraft) => {
    try {
      await createSkill.mutateAsync(draft);
      toast.success(t("Skill created", "Skill erstellt"), { description: draft.name });
      setWizardOpen(false);
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : t("Could not create the skill.", "Skill konnte nicht erstellt werden."),
      );
    }
  };

  const handleRestore = (skill: Skill) => {
    restoreSkill.mutate(skill.id, {
      onSuccess: () => {
        toast.success(t("Skill restored", "Skill wiederhergestellt"), {
          description: skill.name,
        });
      },
      onError: (err: unknown) =>
        toast.error(
          err instanceof Error
            ? err.message
            : t("Could not restore the skill.", "Skill konnte nicht wiederhergestellt werden."),
        ),
    });
  };

  return (
    <div className="space-y-6">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard icon={Sparkles} label={t("Total skills", "Skills gesamt")} value={stats.total} />
        <StatCard
          icon={Blocks}
          label={t("Local library", "Lokale Bibliothek")}
          value={stats.local}
        />
        <StatCard icon={Download} label={t("From store", "Aus Store")} value={stats.store} />
        <StatCard
          icon={Users2}
          label={t("Agent assignments", "Agenten-Zuweisungen")}
          value={stats.assigned}
        />
      </div>

      <Panel className="space-y-3 p-4">
        <ListToolbar
          config={{
            searchPlaceholder: t(
              "Search skills, tools, capabilities…",
              "Skills, Tools, Fähigkeiten suchen…",
            ),
            filters: [
              {
                key: "category",
                label: t("Category", "Kategorie"),
                options: skillCategories.map((c) => ({
                  value: c.id,
                  label: lang === "de" ? c.labelDe : c.label,
                })),
              },
              {
                key: "author",
                label: t("Author", "Autor"),
                options: authorOptions,
              },
            ],
            groupBy: [
              { value: "category", label: t("Category", "Kategorie") },
              { value: "author", label: t("Author", "Autor") },
            ],
            showArchivedToggle: true,
            archivedToggleLabel: t("Show archived", "Archivierte anzeigen"),
          }}
          state={queryState}
          onStateChange={setQueryState}
          totalCount={data?.totalCount ?? 0}
        />
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={() => setWizardOpen(true)}
            className="inline-flex items-center gap-2 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90"
          >
            <Plus className="h-4 w-4" /> {t("New skill", "Neuer Skill")}
          </button>
          <button
            type="button"
            onClick={() => setImportOpen(true)}
            className="inline-flex items-center gap-2 rounded-md border border-border px-3 py-2 text-sm font-medium transition hover:border-primary/60"
          >
            <Download className="h-4 w-4" /> {t("Import from source", "Aus Quelle importieren")}
          </button>
        </div>
      </Panel>

      <div className="space-y-6">
        {grouped.map(({ group, items: groupSkills }) => (
          <div key={group ?? "all"} className="space-y-3">
            {group ? <h3 className="text-xs uppercase text-muted-foreground">{group}</h3> : null}
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {groupSkills.map((s) => (
                <SkillCard
                  key={s.id}
                  skill={s}
                  categoryLabel={catLabel(s.category)}
                  onOpen={() => setSelected(s)}
                  archived={queryState.includeArchived && Boolean(s.deletedAt)}
                  onRestore={() => handleRestore(s)}
                  restoring={restoreSkill.isPending}
                />
              ))}
            </div>
          </div>
        ))}
        {items.length === 0 && (
          <Panel className="p-10 text-center text-sm text-muted-foreground">
            {t("No skills match your filters.", "Keine Skills entsprechen den Filtern.")}
          </Panel>
        )}
      </div>

      {selectedLive && (
        <SkillDrawer
          skill={selectedLive}
          categoryLabel={catLabel(selectedLive.category)}
          onClose={() => setSelected(null)}
        />
      )}

      {importOpen && <ImportWizard onClose={() => setImportOpen(false)} />}

      {wizardOpen && (
        <SkillWizard
          onClose={() => setWizardOpen(false)}
          onCreate={handleCreate}
          busy={createSkill.isPending}
        />
      )}
    </div>
  );
}

function StatCard({
  icon: Icon,
  label,
  value,
}: {
  icon: typeof Sparkles;
  label: string;
  value: number;
}) {
  return (
    <Panel className="flex items-center gap-3 p-4">
      <div className="grid h-10 w-10 place-items-center rounded-md bg-primary/10 text-primary">
        <Icon className="h-5 w-5" />
      </div>
      <div>
        <div className="text-2xl font-serif leading-none">{value}</div>
        <div className="mt-1 text-xs uppercase tracking-wider text-muted-foreground">{label}</div>
      </div>
    </Panel>
  );
}

function SkillCard({
  skill,
  categoryLabel,
  onOpen,
  archived = false,
  onRestore,
  restoring = false,
}: {
  skill: Skill;
  categoryLabel: string;
  onOpen: () => void;
  archived?: boolean;
  onRestore?: () => void;
  restoring?: boolean;
}) {
  const t = useT();
  return (
    <Panel
      className={cn(
        "flex flex-col gap-3 p-4 transition hover:border-primary/40",
        archived && "opacity-60",
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h3 className="truncate font-serif text-lg leading-tight">{skill.name}</h3>
            <span className="rounded-full border border-border px-1.5 py-0.5 micro-label">
              v{skill.version}
            </span>
            {archived && (
              <span className="rounded-full border border-[color:var(--status-warning)]/40 bg-[color:var(--status-warning)]/10 px-1.5 py-0.5 micro-label text-[color:var(--status-warning)]">
                {t("Archived", "Archiviert")}
              </span>
            )}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
            <span className="rounded bg-primary/10 px-1.5 py-0.5 text-primary">
              {categoryLabel}
            </span>
            <span>· {skill.author}</span>
            <span>·</span>
            <span
              className={cn(
                "inline-flex items-center gap-1",
                skill.origin === "store" && "text-[color:var(--status-warning)]",
              )}
            >
              {skill.origin === "store" ? (
                <>
                  <Download className="h-3 w-3" /> Store
                </>
              ) : (
                <>
                  <Blocks className="h-3 w-3" /> {t("Local", "Lokal")}
                </>
              )}
            </span>
          </div>
        </div>
      </div>
      <p className="text-sm text-muted-foreground line-clamp-2">{skill.description}</p>
      <div className="flex flex-wrap gap-1.5">
        {skill.tools.slice(0, 4).map((tool) => (
          <span
            key={tool}
            className="inline-flex items-center gap-1 rounded border border-border bg-background/50 px-1.5 py-0.5 text-micro text-muted-foreground"
          >
            <Wrench className="h-3 w-3" /> {tool}
          </span>
        ))}
      </div>
      <div className="mt-1 flex items-center justify-between border-t border-border/60 pt-3 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <Users2 className="h-3.5 w-3.5" />
          {t(`${skill.usedByAgents} agents`, `${skill.usedByAgents} Agenten`)}
        </span>
        {archived ? (
          <button
            type="button"
            onClick={onRestore}
            disabled={restoring}
            className="rounded-md border border-border px-2.5 py-1 text-xs transition hover:border-primary/50 hover:text-foreground disabled:opacity-50"
          >
            {restoring
              ? t("Restoring…", "Wird wiederhergestellt…")
              : t("Restore", "Wiederherstellen")}
          </button>
        ) : (
          <button
            type="button"
            onClick={onOpen}
            className="rounded-md border border-border px-2.5 py-1 text-xs transition hover:border-primary/50 hover:text-foreground"
          >
            {t("Open", "Öffnen")}
          </button>
        )}
      </div>
    </Panel>
  );
}

export function SkillDrawer({
  skill,
  categoryLabel,
  onClose,
}: {
  skill: Skill;
  categoryLabel: string;
  onClose: () => void;
}) {
  const t = useT();
  const { lang } = useLang();
  // Assignment picker -- needs the whole tenant, not a paginated list view.
  const { data: agentsPage } = useAgents({ pageSize: 200 });
  const agents = agentsPage?.items ?? [];
  const assign = useAssignSkill();
  const updateSkill = useUpdateSkill();
  const [target, setTarget] = useState("");
  const [name, setName] = useState(skill.name);
  const [description, setDescription] = useState(skill.description);
  const [category, setCategory] = useState(skill.category);

  const handleSave = () => {
    if (!name.trim()) return;
    updateSkill.mutate(
      { id: skill.id, name: name.trim(), description: description.trim(), category },
      {
        onSuccess: () => {
          toast.success(t("Skill updated", "Skill aktualisiert"), { description: name.trim() });
        },
        onError: (e: unknown) =>
          toast.error(
            e instanceof Error
              ? e.message
              : t("Could not save changes.", "Änderungen konnten nicht gespeichert werden."),
          ),
      },
    );
  };

  return (
    <DetailSheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={skill.name}
      description={skill.description}
      footer={
        <>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-border px-3 py-2 text-sm text-muted-foreground transition hover:text-foreground"
          >
            {t("Close", "Schließen")}
          </button>
          <button
            type="button"
            disabled={!name.trim() || updateSkill.isPending}
            onClick={handleSave}
            className="rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
          >
            {updateSkill.isPending ? t("Saving…", "Speichern …") : t("Save", "Speichern")}
          </button>
        </>
      }
    >
      <div className="micro-label">
        {categoryLabel} · v{skill.version}
      </div>
      <p className="mt-1 text-xs text-muted-foreground">{skill.author}</p>

      <Section icon={Sparkles} title={t("Edit skill", "Skill bearbeiten")}>
        <div className="space-y-3">
          <Field label={t("Name", "Name")}>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:border-primary/60"
            />
          </Field>
          <Field label={t("Description", "Beschreibung")}>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
              className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:border-primary/60"
            />
          </Field>
          <Field label={t("Category", "Kategorie")}>
            <select
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:border-primary/60"
            >
              {skillCategories.map((c) => (
                <option key={c.id} value={c.id}>
                  {lang === "de" ? c.labelDe : c.label}
                </option>
              ))}
              {!skillCategories.some((c) => c.id === category) && (
                <option value={category}>{category}</option>
              )}
            </select>
          </Field>
        </div>
      </Section>

      <Section icon={BookOpen} title={t("Instructions", "Anleitung")}>
        <p className="text-sm text-muted-foreground">{skill.instructions}</p>
      </Section>

      <Section icon={Wrench} title={t("Required tools (MCP)", "Benötigte Tools (MCP)")}>
        <div className="flex flex-wrap gap-1.5">
          {skill.tools.map((tool) => (
            <span
              key={tool}
              className="rounded border border-border bg-background/50 px-2 py-1 text-xs"
            >
              {tool}
            </span>
          ))}
        </div>
      </Section>

      {skill.knowledge && skill.knowledge.length > 0 && (
        <Section icon={BookOpen} title={t("Knowledge bases", "Wissensbasen")}>
          <ul className="space-y-1 text-sm text-muted-foreground">
            {skill.knowledge.map((k) => (
              <li key={k} className="flex items-center gap-2">
                <Check className="h-3.5 w-3.5 text-primary" /> {k}
              </li>
            ))}
          </ul>
        </Section>
      )}

      <Section icon={ShieldAlert} title={t("Guardrails", "Guardrails")}>
        <ul className="space-y-1 text-sm text-muted-foreground">
          {skill.guardrails.map((g) => (
            <li key={g} className="flex items-start gap-2">
              <ShieldAlert className="mt-0.5 h-3.5 w-3.5 text-[color:var(--status-warning)]" />
              <span>{g}</span>
            </li>
          ))}
        </ul>
      </Section>

      <Section icon={Users2} title={t("Assign to agent", "An Agent zuweisen")}>
        {/* This used to be a button that showed "Skill assigned" and closed
            the panel without calling anything -- a leftover from when the
            screen ran on mock data. Assigning needs a target and a version,
            so both are here. */}
        <div className="flex gap-2">
          <select
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            className="min-w-0 flex-1 rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:border-primary/60"
          >
            <option value="">{t("Choose an agent…", "Agent wählen…")}</option>
            {agents.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
                {a.role ? ` · ${a.role}` : ""}
              </option>
            ))}
          </select>
          <button
            type="button"
            disabled={!target || !skill.currentVersionId || assign.isPending}
            onClick={() => {
              if (!target || !skill.currentVersionId) return;
              assign.mutate(
                { agentId: target, skillVersionId: skill.currentVersionId },
                {
                  onSuccess: (res) => {
                    const already = res.status === "already_assigned";
                    toast.success(
                      already
                        ? t("Already assigned", "War bereits zugewiesen")
                        : t("Skill assigned", "Skill zugewiesen"),
                      {
                        description: `${skill.name} → ${
                          agents.find((a) => a.id === target)?.name ?? target
                        }`,
                      },
                    );
                    onClose();
                  },
                  // The frame decides what an agent may be given, so a refusal
                  // here is information, not a glitch: it says the skill needs
                  // a tool this agent is not granted.
                  onError: (e: unknown) =>
                    toast.error(
                      e instanceof Error
                        ? e.message
                        : t("Could not assign", "Zuweisen fehlgeschlagen"),
                    ),
                },
              );
            }}
            className="rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
          >
            {assign.isPending ? t("Assigning…", "Weise zu…") : t("Assign", "Zuweisen")}
          </button>
        </div>
        {!skill.currentVersionId && (
          <p className="mt-2 text-xs text-[color:var(--status-warning)]">
            {t(
              "This skill has no published version yet, so there is nothing to assign.",
              "Dieser Skill hat noch keine veröffentlichte Version — es gibt nichts zuzuweisen.",
            )}
          </p>
        )}
      </Section>
    </DetailSheet>
  );
}

function Section({
  icon: Icon,
  title,
  children,
}: {
  icon: typeof Sparkles;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mt-5 border-t border-border/60 pt-4">
      <h3 className="mb-2 flex items-center gap-2 text-xs uppercase tracking-widest text-muted-foreground">
        <Icon className="h-3.5 w-3.5" /> {title}
      </h3>
      {children}
    </div>
  );
}

function SkillWizard({
  onClose,
  onCreate,
  busy,
}: {
  onClose: () => void;
  onCreate: (s: SkillDraft) => void;
  busy: boolean;
}) {
  const t = useT();
  const { lang } = useLang();
  const [step, setStep] = useState(0);
  const [form, setForm] = useState({
    name: "",
    description: "",
    category: "operations" as SkillCategory,
    instructions: "",
    tools: "",
    guardrails: "",
  });

  const canNext =
    (step === 0 && form.name.trim() && form.description.trim()) ||
    (step === 1 && form.instructions.trim()) ||
    step === 2 ||
    step === 3;

  const submit = () => {
    onCreate({
      name: form.name.trim(),
      description: form.description.trim(),
      category: form.category,
      instructions: form.instructions.trim(),
      tools: form.tools
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
      guardrails: form.guardrails
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean),
    });
  };

  const steps = [
    t("Basics", "Basis"),
    t("Instructions", "Anleitung"),
    t("Tools & Guardrails", "Tools & Guardrails"),
    t("Review", "Prüfen"),
  ];

  return (
    <div className="fixed inset-0 z-40 grid place-items-center bg-black/60 p-4 backdrop-blur-sm">
      <div className="w-full max-w-xl overflow-hidden rounded-xl border border-border bg-panel shadow-2xl">
        <div className="flex items-center justify-between border-b border-border px-5 py-4">
          <div>
            <div className="micro-label">{t("New skill", "Neuer Skill")}</div>
            <h2 className="font-serif text-xl">{steps[step]}</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="grid h-8 w-8 place-items-center rounded-md border border-border text-muted-foreground transition hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="flex gap-1 px-5 pt-3">
          {steps.map((_, i) => (
            <span
              key={i}
              className={cn(
                "h-1 flex-1 rounded-full transition",
                i <= step ? "bg-primary" : "bg-border",
              )}
            />
          ))}
        </div>
        <div className="space-y-3 p-5">
          {step === 0 && (
            <>
              <Field label={t("Name", "Name")}>
                <input
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:border-primary/60"
                  placeholder={t("e.g. Invoice Review", "z. B. Rechnung prüfen")}
                />
              </Field>
              <Field label={t("Description", "Beschreibung")}>
                <textarea
                  value={form.description}
                  onChange={(e) => setForm({ ...form, description: e.target.value })}
                  rows={3}
                  className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:border-primary/60"
                />
              </Field>
              <Field label={t("Category", "Kategorie")}>
                <select
                  value={form.category}
                  onChange={(e) => setForm({ ...form, category: e.target.value as SkillCategory })}
                  className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:border-primary/60"
                >
                  {skillCategories.map((c) => (
                    <option key={c.id} value={c.id}>
                      {lang === "de" ? c.labelDe : c.label}
                    </option>
                  ))}
                </select>
              </Field>
            </>
          )}
          {step === 1 && (
            <Field label={t("Prompt / logic", "Prompt / Logik")}>
              <textarea
                value={form.instructions}
                onChange={(e) => setForm({ ...form, instructions: e.target.value })}
                rows={8}
                className="w-full rounded-md border border-border bg-background px-3 py-2 font-mono text-xs outline-none focus:border-primary/60"
                placeholder={t(
                  "Describe step by step what the skill should do…",
                  "Beschreibe Schritt für Schritt, was der Skill tun soll…",
                )}
              />
            </Field>
          )}
          {step === 2 && (
            <>
              <Field label={t("Tools (comma-separated)", "Tools (kommagetrennt)")}>
                <input
                  value={form.tools}
                  onChange={(e) => setForm({ ...form, tools: e.target.value })}
                  placeholder="Accounting, ERP, OCR"
                  className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:border-primary/60"
                />
              </Field>
              <Field label={t("Guardrails (one per line)", "Guardrails (eine pro Zeile)")}>
                <textarea
                  value={form.guardrails}
                  onChange={(e) => setForm({ ...form, guardrails: e.target.value })}
                  rows={4}
                  className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:border-primary/60"
                />
              </Field>
            </>
          )}
          {step === 3 && (
            <div className="space-y-2 text-sm">
              <Row k={t("Name", "Name")} v={form.name} />
              <Row k={t("Category", "Kategorie")} v={form.category} />
              <Row k={t("Tools", "Tools")} v={form.tools || "—"} />
              <Row
                k={t("Guardrails", "Guardrails")}
                v={form.guardrails.split("\n").filter(Boolean).length + ""}
              />
            </div>
          )}
        </div>
        <div className="flex justify-between border-t border-border px-5 py-3">
          <button
            type="button"
            onClick={() => (step === 0 ? onClose() : setStep(step - 1))}
            className="rounded-md border border-border px-3 py-1.5 text-sm text-muted-foreground transition hover:text-foreground"
          >
            {step === 0 ? t("Cancel", "Abbrechen") : t("Back", "Zurück")}
          </button>
          <button
            type="button"
            disabled={!canNext || busy}
            onClick={() => (step === 3 ? submit() : setStep(step + 1))}
            className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:opacity-40"
          >
            {step === 3
              ? busy
                ? t("Creating…", "Wird erstellt …")
                : t("Create", "Erstellen")
              : t("Continue", "Weiter")}
          </button>
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block micro-label">{label}</span>
      {children}
    </label>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-border/50 pb-1.5">
      <span className="text-xs uppercase tracking-widest text-muted-foreground">{k}</span>
      <span className="text-right text-sm">{v}</span>
    </div>
  );
}
/**
 * Bring skills in from a public source.
 *
 * The screen is built around the VERDICT rather than the list, because the two
 * things that go wrong here are invisible in a list of names.
 *
 * A skill's instruction arrives at the agent as a tool result and stays in its
 * transcript for the rest of the run, so its size is paid out of the model's
 * context window — the same text is unremarkable behind a large model and fatal
 * behind a small one. That is why every row shows what it costs against THIS
 * department's budget, and why the department picker matters.
 *
 * And a skill is an instruction the agent will follow. Importing one from a
 * stranger's repository is taking instructions from a stranger, so the text is
 * shown, not merely its name — a preview nobody can read is not a preview.
 */
function ImportWizard({ onClose }: { onClose: () => void }) {
  const t = useT();
  const [source, setSource] = useState("");
  const [departmentId, setDepartmentId] = useState<string>("");
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  // Empty means "let the department decide". A number here overrides it for
  // this look only, so an exception never quietly becomes the new normal.
  const [budgetOverride, setBudgetOverride] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  // Import-target picker -- needs the whole tenant, not a paginated list view.
  const { data: departmentsPage } = useDepartments({ pageSize: 200 });
  const departments = departmentsPage?.items ?? [];
  const preview = usePreviewSkillImport();
  const runImport = useImportSkills();
  const found = preview.data?.skills ?? [];
  // The budget in force RIGHT NOW: what you typed wins over what the last fetch
  // was judged against. Without this the field only took effect on the next
  // "Look", so typing 5000 left every row still measured against 1000 — which
  // reads as "the number is ignored".
  const budget = Number(budgetOverride) || preview.data?.budgetTokens || 0;

  // Mirrors parse_skill() in backend/src/oc8/skills/importer.py. Recomputed here
  // rather than refetched so moving the number is instant and costs a stranger's
  // server nothing; the backend still decides at import time.
  // No budget means NO ceiling, not a ceiling of zero. Getting this backwards
  // would call every skill too big the moment nobody had recorded a window --
  // which is precisely when oc8 knows least and should judge least.
  const verdictOf = (tokens: number) =>
    budget <= 0 ? "fits" : tokens > budget ? "too_big" : tokens > budget / 2 ? "tight" : "fits";
  const oversizedChosen = found.filter(
    (c) => chosen.has(c.name) && verdictOf(c.tokens) === "too_big",
  ).length;

  const toggle = (name: string) =>
    setChosen((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });

  const look = () => {
    setChosen(new Set());
    preview.mutate(
      {
        source: source.trim(),
        departmentId: departmentId || null,
        budgetTokens: Number(budgetOverride) || null,
      },
      {
        onError: (e: unknown) =>
          toast.error(
            e instanceof Error ? e.message : t("Could not read that source", "Quelle nicht lesbar"),
          ),
      },
    );
  };

  const submit = () => {
    runImport.mutate(
      {
        source: source.trim(),
        names: [...chosen],
        departmentId: departmentId || null,
        budgetTokens: Number(budgetOverride) || null,
        // The backend refuses an oversized skill unless told otherwise. Ticking
        // one IS being told otherwise -- the size is shown in the row, so a tick
        // is a decision rather than an oversight.
        acceptOversized: found.some((c) => chosen.has(c.name) && verdictOf(c.tokens) === "too_big"),
      },
      {
        onSuccess: (res) => {
          if (res.imported.length)
            toast.success(
              t(
                `${res.imported.length} skill(s) imported`,
                `${res.imported.length} Skill(s) importiert`,
              ),
            );
          // Every refusal is named. A silent skip reads as success and is found
          // weeks later, when an agent is missing a procedure nobody removed.
          res.skipped.forEach((s) => toast.warning(`${s.name}: ${s.reason}`));
          if (res.imported.length) onClose();
        },
        onError: (e: unknown) =>
          toast.error(e instanceof Error ? e.message : t("Import failed", "Import fehlgeschlagen")),
      },
    );
  };

  return (
    <div className="fixed inset-0 z-40 grid place-items-center bg-black/60 p-4 backdrop-blur-sm">
      <div className="flex max-h-[85vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-border bg-panel shadow-2xl">
        <div className="flex items-center justify-between border-b border-border px-5 py-4">
          <div>
            <div className="micro-label">{t("Skills", "Skills")}</div>
            <div className="text-base font-semibold">
              {t("Import from a source", "Aus einer Quelle importieren")}
            </div>
          </div>
          <button type="button" onClick={onClose} className="rounded p-1 hover:bg-muted">
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="space-y-4 overflow-y-auto px-5 py-4">
          <div className="space-y-1">
            <label className="text-xs font-medium text-muted-foreground">
              {t("Git repository or archive", "Git-Repository oder Archiv")}
            </label>
            <div className="flex gap-2">
              <input
                value={source}
                onChange={(e) => setSource(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && source.trim() && look()}
                placeholder="https://github.com/owner/repo"
                className="flex-1 rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:border-primary/60"
              />
              <button
                type="button"
                disabled={!source.trim() || preview.isPending}
                onClick={look}
                className="inline-flex items-center gap-2 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
              >
                <Search className="h-4 w-4" />
                {preview.isPending ? t("Reading…", "Lese…") : t("Look", "Ansehen")}
              </button>
            </div>
            <p className="text-xs text-muted-foreground">
              {t(
                "Nothing is imported until you choose. The source is fetched, not trusted.",
                "Es wird nichts importiert, bis du auswählst. Die Quelle wird gelesen, nicht geglaubt.",
              )}
            </p>
          </div>

          <div className="space-y-1">
            <label className="text-xs font-medium text-muted-foreground">
              {t(
                "Judge against this department's model",
                "Gegen das Modell dieser Abteilung prüfen",
              )}
            </label>
            <select
              value={departmentId}
              onChange={(e) => setDepartmentId(e.target.value)}
              className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:border-primary/60"
            >
              <option value="">
                {t("No department (strict default)", "Keine Abteilung (strenger Standard)")}
              </option>
              {departments.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name}
                </option>
              ))}
            </select>
          </div>

          <div className="space-y-1">
            <label className="text-xs font-medium text-muted-foreground">
              {t("Budget per skill", "Budget je Skill")}
            </label>
            <div className="flex items-center gap-2">
              <input
                type="number"
                min={1}
                value={budgetOverride}
                onChange={(e) => setBudgetOverride(e.target.value)}
                placeholder={
                  preview.data ? String(budget) : t("from the department", "aus der Abteilung")
                }
                className="w-40 rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:border-primary/60"
              />
              <span className="text-xs text-muted-foreground">{t("tokens", "Token")}</span>
              {budgetOverride && (
                <button
                  type="button"
                  onClick={() => setBudgetOverride("")}
                  className="text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground"
                >
                  {t("use the department's", "Abteilung verwenden")}
                </button>
              )}
            </div>
            <p className="text-xs text-muted-foreground">
              {budgetOverride
                ? t(
                    "Judging against your number for this look only — nothing is saved.",
                    "Es wird nur für diese Ansicht gegen deine Zahl geprüft — nichts wird gespeichert.",
                  )
                : t(
                    "Empty means no limit. A number here, or a context window recorded on the model, is what makes size a verdict at all.",
                    "Leer heißt: keine Grenze. Erst eine Zahl hier — oder ein am Modell hinterlegtes Kontextfenster — macht die Größe zu einem Urteil.",
                  )}
            </p>
          </div>

          {preview.data && (
            <div className="space-y-2">
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span className="flex items-center gap-2">
                  <span>
                    {found.length} {t("skills found", "Skills gefunden")}
                  </span>
                  {found.length > 0 && (
                    <button
                      type="button"
                      onClick={() =>
                        setChosen(
                          chosen.size === found.length
                            ? new Set()
                            : new Set(found.map((c) => c.name)),
                        )
                      }
                      className="underline underline-offset-2 hover:text-foreground"
                    >
                      {chosen.size === found.length
                        ? t("select none", "keinen auswählen")
                        : t("select all", "alle auswählen")}
                    </button>
                  )}
                </span>
                <span>
                  {budget > 0
                    ? `${t("Budget", "Budget")}: ${budget} ${t("tokens per skill", "Token je Skill")}`
                    : t(
                        "No budget — nothing is refused for its size",
                        "Kein Budget — nichts wird wegen seiner Größe abgelehnt",
                      )}
                </span>
              </div>
              {found.length === 0 && (
                <p className="rounded-md border border-border p-3 text-sm text-muted-foreground">
                  {t(
                    "No SKILL.md found there. This importer reads Claude Code skills.",
                    "Dort liegt keine SKILL.md. Dieser Import liest Claude-Code-Skills.",
                  )}
                </p>
              )}
              {found.map((c) => (
                <CandidateRow
                  key={c.name}
                  candidate={c}
                  budget={budget}
                  verdict={verdictOf(c.tokens)}
                  checked={chosen.has(c.name)}
                  onToggle={() => toggle(c.name)}
                  expanded={open === c.name}
                  onExpand={() => setOpen(open === c.name ? null : c.name)}
                />
              ))}
            </div>
          )}
        </div>

        <div className="flex items-center justify-between border-t border-border px-5 py-3">
          <span className="text-xs text-muted-foreground">
            {chosen.size > 0
              ? oversizedChosen > 0
                ? t(
                    `${chosen.size} selected, ${oversizedChosen} over budget`,
                    `${chosen.size} ausgewählt, ${oversizedChosen} über Budget`,
                  )
                : t(`${chosen.size} selected`, `${chosen.size} ausgewählt`)
              : t("Select what to import", "Wähle aus, was importiert wird")}
          </span>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-md border border-border px-3 py-2 text-sm hover:border-primary/60"
            >
              {t("Cancel", "Abbrechen")}
            </button>
            <button
              type="button"
              disabled={chosen.size === 0 || runImport.isPending}
              onClick={submit}
              className="inline-flex items-center gap-2 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
            >
              <Download className="h-4 w-4" />
              {runImport.isPending ? t("Importing…", "Importiere…") : t("Import", "Importieren")}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

/** One candidate: what it is, what it costs, and what oc8 could not check. */
function CandidateRow({
  candidate,
  budget,
  verdict,
  checked,
  onToggle,
  expanded,
  onExpand,
}: {
  candidate: ImportCandidate;
  budget: number;
  verdict: string;
  checked: boolean;
  onToggle: () => void;
  expanded: boolean;
  onExpand: () => void;
}) {
  const t = useT();
  const tooBig = verdict === "too_big";
  const verdictLabel =
    verdict === "fits"
      ? t("fits", "passt")
      : verdict === "tight"
        ? t("tight", "knapp")
        : t("too big", "zu groß");

  return (
    <div
      className={cn(
        "rounded-md border p-3",
        tooBig ? "border-destructive/40 bg-destructive/5" : "border-border",
      )}
    >
      <div className="flex items-start gap-3">
        <input type="checkbox" checked={checked} onChange={onToggle} className="mt-1 h-4 w-4" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="truncate text-sm font-medium">{candidate.name}</span>
            {budget > 0 && (
              <span
                className={cn(
                  "rounded px-1.5 py-0.5 micro-label",
                  verdict === "fits"
                    ? "bg-primary/10 text-primary"
                    : verdict === "tight"
                      ? "bg-amber-500/15 text-amber-600"
                      : "bg-destructive/15 text-destructive",
                )}
              >
                {verdictLabel}
              </span>
            )}
            <span className="text-xs text-muted-foreground">
              {budget > 0
                ? `${candidate.tokens} / ${budget} ${t("tokens", "Token")}`
                : `${candidate.tokens} ${t("tokens", "Token")}`}
            </span>
          </div>
          {candidate.description && (
            <p className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">
              {candidate.description}
            </p>
          )}
          {candidate.warnings.map((w) => (
            <p key={w} className="mt-1 flex items-start gap-1.5 text-xs text-amber-600">
              <ShieldAlert className="mt-px h-3 w-3 shrink-0" />
              <span>{w}</span>
            </p>
          ))}
          {tooBig && (
            <p className="mt-1 text-xs text-destructive">
              {t(
                "Over the budget: it stays in the transcript for the whole run. You can still import it — raise the budget above, or take it knowingly.",
                "Über dem Budget: es bleibt den ganzen Lauf im Verlauf stehen. Du kannst es trotzdem importieren — Budget oben anheben oder es bewusst nehmen.",
              )}
            </p>
          )}
          <button
            type="button"
            onClick={onExpand}
            className="mt-1.5 text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground"
          >
            {expanded
              ? t("Hide the text", "Text ausblenden")
              : t("Read the text before importing", "Text vor dem Import lesen")}
          </button>
          {expanded && (
            <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap rounded border border-border bg-background p-2 text-xs leading-relaxed">
              {candidate.instruction}
            </pre>
          )}
        </div>
      </div>
    </div>
  );
}
