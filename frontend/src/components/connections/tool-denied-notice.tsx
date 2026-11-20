import { Link } from "@tanstack/react-router";
import type { ToolDeniedInfo } from "@/lib/connections";
import { useMay, useMayManageAgent } from "@/lib/governance-hooks";
import { useT } from "@/lib/i18n";

/** A tool call the guardrails withhold: say which tool and where it is
 *  allowed -- the department's guardrails, or the agent's when only the
 *  agent's own narrowing withholds it. The link goes to that tab, for people
 *  who may change it. Shown next to ConnectionRequiredNotice. */
export function ToolDeniedNotice({ value }: { value?: ToolDeniedInfo | null }) {
  // Every chat turn mounts this; only a denial reads the caller's grants.
  if (!value) return null;
  return <ToolDeniedLine value={value} />;
}

function ToolDeniedLine({ value }: { value: ToolDeniedInfo }) {
  const t = useT();
  const may = useMay();
  const mayManageAgent = useMayManageAgent();
  const system = value.label || value.connection;
  const agentScope = value.scope === "agent";
  let text: string;
  if (value.tool) {
    text = agentScope
      ? t(
          `This agent's guardrails do not allow the tool “${value.tool}” (${system}). Whoever may manage the agent can enable it under Agent → Guardrails.`,
          `Die Guardrails dieses Agenten erlauben das Werkzeug „${value.tool}“ (${system}) nicht. Wer den Agenten verwalten darf, kann es unter Agent → Guardrails freigeben.`,
        )
      : t(
          `This department does not allow the tool “${value.tool}” (${system}). An admin can enable it under Department → Guardrails.`,
          `Diese Abteilung erlaubt das Werkzeug „${value.tool}“ (${system}) nicht. Ein Admin kann es unter Abteilung → Guardrails freigeben.`,
        );
  } else {
    text = agentScope
      ? t(
          `This agent's guardrails do not allow the connection ${system}. Whoever may manage the agent can enable it under Agent → Guardrails.`,
          `Die Guardrails dieses Agenten erlauben die Verbindung ${system} nicht. Wer den Agenten verwalten darf, kann sie unter Agent → Guardrails freigeben.`,
        )
      : t(
          `This department does not allow the connection ${system}. An admin can enable it under Department → Guardrails.`,
          `Diese Abteilung erlaubt die Verbindung ${system} nicht. Ein Admin kann sie unter Abteilung → Guardrails freigeben.`,
        );
  }
  const linkLabel = t("Open guardrails →", "Guardrails öffnen →");
  const departmentLink =
    !agentScope && value.departmentId && may("department:manage") ? value.departmentId : null;
  const agentLink =
    agentScope && value.agentId && mayManageAgent(value.departmentId) ? value.agentId : null;
  return (
    <div
      role="alert"
      className="flex flex-wrap items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm"
    >
      <span>{text}</span>
      {departmentLink && (
        // The department page reads `?tab=` once on arrival (departments.$id.tsx).
        <Link
          to="/departments/$id"
          params={{ id: departmentLink }}
          search={{ tab: "guardrails" }}
          className={LINK}
        >
          {linkLabel}
        </Link>
      )}
      {agentLink && (
        <Link
          to="/agents/$id"
          params={{ id: agentLink }}
          search={{ tab: "guardrails" }}
          className={LINK}
        >
          {linkLabel}
        </Link>
      )}
    </div>
  );
}

const LINK =
  "font-medium underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2";
