import { useState } from "react";
import { Panel } from "@/components/app-shell";
import { useT } from "@/lib/i18n";
import { useDepartments } from "@/lib/hooks";
import { AgentIdentityFields, type AgentIdentity } from "@/components/agent-identity-fields";

export function AgentIdentityStep({
  departmentId,
  onDone,
}: {
  departmentId: string;
  onDone: (identity: AgentIdentity) => void;
}) {
  const t = useT();
  // Department picker for the wizard, not a paginated list view.
  const { data: departmentsPage } = useDepartments({ pageSize: 200 });
  const departments = departmentsPage?.items ?? [];
  const [identity, setIdentity] = useState<AgentIdentity>({
    name: "",
    role: "",
    description: "",
    departmentId,
    avatarIdx: 0,
  });

  const canContinue = identity.name.trim().length > 0 && identity.role.trim().length > 0;

  return (
    <Panel className="space-y-4 p-6">
      <AgentIdentityFields value={identity} onChange={setIdentity} departments={departments} />
      <button
        onClick={() => canContinue && onDone(identity)}
        disabled={!canContinue}
        className="w-full rounded-md bg-brand-gradient px-4 py-2 text-sm font-medium text-brand-gradient-foreground transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-60"
      >
        {t("Continue", "Weiter")}
      </button>
    </Panel>
  );
}
