import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { useT } from "@/lib/i18n";
import { useOrganizationSettings, useUpdateOrganizationSettings } from "@/lib/hooks";

export function OrgStep({ onDone }: { onDone: () => void }) {
  const t = useT();
  const { data: org } = useOrganizationSettings();
  const update = useUpdateOrganizationSettings();
  const [name, setName] = useState("");
  const [region, setRegion] = useState("");

  useEffect(() => {
    if (org) {
      setName((current) => current || org.name);
      setRegion((current) => current || org.region);
    }
  }, [org]);

  const submit = async () => {
    try {
      await update.mutateAsync({ name, region });
      onDone();
    } catch (err) {
      toast.error(
        t("Could not save workspace name", "Workspace-Name konnte nicht gespeichert werden"),
        {
          description: err instanceof Error ? err.message : String(err),
        },
      );
    }
  };

  return (
    <Panel className="space-y-4 p-6">
      <label className="block text-xs uppercase tracking-wider text-muted-foreground">
        {t("Company / workspace name", "Firmen-/Workspace-Name")}
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm text-foreground outline-none focus:border-primary/50"
        />
      </label>
      <label className="block text-xs uppercase tracking-wider text-muted-foreground">
        {t("Region", "Region")}
        <input
          value={region}
          onChange={(e) => setRegion(e.target.value)}
          placeholder="eu"
          className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm text-foreground outline-none focus:border-primary/50"
        />
      </label>
      <button
        onClick={submit}
        disabled={update.isPending || !name.trim() || !region.trim()}
        className="w-full rounded-md bg-brand-gradient px-4 py-2 text-sm font-medium text-brand-gradient-foreground transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-60"
      >
        {update.isPending ? t("Saving…", "Wird gespeichert …") : t("Continue", "Weiter")}
      </button>
    </Panel>
  );
}
