import { AgentAvatar, hueFromOklch } from "@/components/agent-avatar";
import { useT } from "@/lib/i18n";

export const AVATAR_COLORS = [
  "oklch(0.75 0.15 30)",
  "oklch(0.70 0.15 220)",
  "oklch(0.70 0.14 155)",
  "oklch(0.72 0.14 320)",
  "oklch(0.75 0.15 85)",
  "oklch(0.72 0.16 200)",
  "oklch(0.72 0.15 260)",
];

export interface AgentIdentity {
  name: string;
  role: string;
  description: string;
  departmentId: string;
  avatarIdx: number;
}

export function AgentIdentityFields({
  value,
  onChange,
  departments,
}: {
  value: AgentIdentity;
  onChange: (next: AgentIdentity) => void;
  departments: Array<{ id: string; name: string }>;
}) {
  const t = useT();
  return (
    <div className="grid gap-4 md:grid-cols-[auto_minmax(0,1fr)]">
      <div className="flex flex-col items-center gap-3">
        <AgentAvatar
          seed={value.name.trim() || "agent"}
          size={80}
          background="squircle"
          hue={hueFromOklch(AVATAR_COLORS[value.avatarIdx])}
        />
        <button
          type="button"
          onClick={() =>
            onChange({ ...value, avatarIdx: (value.avatarIdx + 1) % AVATAR_COLORS.length })
          }
          className="text-[10px] uppercase tracking-widest text-muted-foreground hover:text-primary"
        >
          {t("Change color", "Farbe ändern")}
        </button>
      </div>
      <div className="space-y-4">
        <Field label={t("Name", "Name")}>
          <input
            value={value.name}
            onChange={(e) => onChange({ ...value, name: e.target.value })}
            placeholder={t("e.g. Nova, Atlas, Miro …", "z. B. Nova, Atlas, Miro …")}
            autoFocus
            className="w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
          />
        </Field>
        <Field label={t("Role", "Rolle")}>
          <input
            value={value.role}
            onChange={(e) => onChange({ ...value, role: e.target.value })}
            placeholder={t(
              "e.g. Customer service, research, marketing …",
              "z. B. Kundenservice, Recherche, Marketing …",
            )}
            className="w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
          />
        </Field>
        <Field label={t("Department", "Abteilung")}>
          <select
            value={value.departmentId}
            onChange={(e) => onChange({ ...value, departmentId: e.target.value })}
            className="w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
          >
            {departments.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t("Instructions", "Anweisungen")}>
          <textarea
            value={value.description}
            onChange={(e) => onChange({ ...value, description: e.target.value })}
            rows={4}
            placeholder={t(
              "What should this agent do? This becomes its standing instructions — you can refine them later on the agent's own page.",
              "Was soll dieser Agent tun? Das wird zu seinen dauerhaften Anweisungen — du kannst sie später auf der Agent-Seite verfeinern.",
            )}
            className="w-full resize-none rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
          />
        </Field>
      </div>
    </div>
  );
}

export function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <div className="mb-1.5 text-xs uppercase tracking-wider text-muted-foreground">{label}</div>
      {children}
    </label>
  );
}
