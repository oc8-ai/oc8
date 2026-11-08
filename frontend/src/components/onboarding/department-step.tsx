import {
  Building2,
  Code2,
  Coins,
  Headphones,
  Megaphone,
  TrendingUp,
  UserRound,
} from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { useT } from "@/lib/i18n";
import { useUpdateDepartment } from "@/lib/hooks";
import { cn } from "@/lib/utils";

const ICON_OPTIONS = [
  { key: "sales", Icon: TrendingUp },
  { key: "engineering", Icon: Code2 },
  { key: "marketing", Icon: Megaphone },
  { key: "finance", Icon: Coins },
  { key: "hr", Icon: UserRound },
  { key: "support", Icon: Headphones },
  { key: "building", Icon: Building2 },
] as const;

export function DepartmentStep({
  department,
  onDone,
}: {
  department: { id: string; name: string; goal: string; icon: string };
  onDone: () => void;
}) {
  const t = useT();
  const update = useUpdateDepartment(department.id);
  const [name, setName] = useState(department.name);
  const [goal, setGoal] = useState(department.goal);
  const [icon, setIcon] = useState(department.icon);

  const submit = async () => {
    try {
      await update.mutateAsync({ name, goal, icon });
      onDone();
    } catch (err) {
      toast.error(t("Could not save department", "Abteilung konnte nicht gespeichert werden"), {
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  return (
    <Panel className="space-y-4 p-6">
      <label className="block text-xs uppercase tracking-wider text-muted-foreground">
        {t("Department name", "Abteilungsname")}
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm text-foreground outline-none focus:border-primary/50"
        />
      </label>
      <label className="block text-xs uppercase tracking-wider text-muted-foreground">
        {t("What does it do?", "Was macht sie?")}
        <input
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          placeholder={t("e.g. Close deals", "z. B. Verträge abschließen")}
          className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm text-foreground outline-none focus:border-primary/50"
        />
      </label>
      <div>
        <div className="mb-1.5 text-xs uppercase tracking-wider text-muted-foreground">
          {t("Icon", "Symbol")}
        </div>
        <div className="flex flex-wrap gap-2">
          {ICON_OPTIONS.map(({ key, Icon }) => (
            <button
              key={key}
              type="button"
              onClick={() => setIcon(key)}
              className={cn(
                "grid h-10 w-10 place-items-center rounded-lg border transition",
                icon === key
                  ? "border-primary bg-primary/15 text-primary"
                  : "border-border bg-background/30 text-muted-foreground hover:border-primary/40",
              )}
            >
              <Icon className="h-4 w-4" />
            </button>
          ))}
        </div>
      </div>
      <button
        onClick={submit}
        disabled={update.isPending || !name.trim()}
        className="w-full rounded-md bg-brand-gradient px-4 py-2 text-sm font-medium text-brand-gradient-foreground transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-60"
      >
        {update.isPending ? t("Saving…", "Wird gespeichert …") : t("Continue", "Weiter")}
      </button>
    </Panel>
  );
}
