import { useEffect, useState } from "react";
import { ShieldAlert } from "lucide-react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { fill } from "@/components/connections/texts";
import { useT } from "@/lib/i18n";
import { AUTONOMY_ANCHOR } from "@/lib/autonomy";
import {
  useSetAgentAutonomy,
  type AgentAutonomy,
  type AgentDetail,
} from "@/lib/hooks-agent-detail";

const LEVELS: AgentAutonomy[] = ["conservative", "default", "autonomous"];

// How far an agent acts before asking a person (definition.autonomy) and
// whether it may then reach people outside unasked (the `outward` grant):
// the two conditions `b_approval.posture` checks. Guardrails limit WHAT an
// agent may do; this decides whether it does it without asking, so it is a
// choice of its own and never a side effect of picking a guardrail preset.
export function AgentAutonomyPanel({
  agent,
  mayManage,
}: {
  agent: Pick<AgentDetail, "id" | "name" | "autonomy" | "autonomyGrants">;
  mayManage: boolean;
}) {
  const t = useT();
  const save = useSetAgentAutonomy(agent.id);
  const savedLevel = agent.autonomy ?? "default";
  const savedOutward = (agent.autonomyGrants ?? []).includes("outward");
  const [level, setLevel] = useState<AgentAutonomy>(savedLevel);
  const [outward, setOutward] = useState(savedOutward);
  useEffect(() => {
    setLevel(savedLevel);
    setOutward(savedOutward);
  }, [savedLevel, savedOutward]);
  // Opened from a guardrail hint's link: bring this panel into view.
  useEffect(() => {
    if (typeof window === "undefined" || window.location.hash !== `#${AUTONOMY_ANCHOR}`) return;
    document.getElementById(AUTONOMY_ANCHOR)?.scrollIntoView?.({ block: "start" });
  }, []);

  const outwardOn = level === "autonomous" && outward;
  const dirty = level !== savedLevel || outwardOn !== (savedLevel === "autonomous" && savedOutward);
  // Grants this panel does not show (set some other way): kept as they are.
  const otherGrants = (agent.autonomyGrants ?? []).filter((grant) => grant !== "outward");

  const labels: Record<AgentAutonomy, { title: string; body: string }> = {
    conservative: {
      title: t("Ask before every change", "Vor jeder Änderung fragen"),
      body: t(
        "Every change and every message waits for a person to approve it.",
        "Jede Änderung und jede Nachricht wartet, bis ein Mensch sie freigibt.",
      ),
    },
    default: {
      title: t("Standard", "Standard"),
      body: t(
        "Changes inside your systems run on their own. Anything that reaches people outside, deletes something or can't be undone waits for a person.",
        "Änderungen in euren Systemen laufen selbstständig. Alles, was Menschen außerhalb erreicht, etwas löscht oder sich nicht rückgängig machen lässt, wartet auf einen Menschen.",
      ),
    },
    autonomous: {
      title: t("Autonomous", "Selbstständig"),
      body: t(
        "Deleting and other irreversible actions also run without asking, unless a guardrail requires approval. Reaching people outside still asks unless you allow it below.",
        "Auch Löschen und andere nicht umkehrbare Aktionen laufen ohne Rückfrage, außer eine Guardrail verlangt eine Freigabe. Nach außen fragt der Agent weiterhin, außer du erlaubst es unten.",
      ),
    },
  };

  function onSave() {
    save.mutate(
      {
        autonomy: level,
        // Below autonomous no grant means anything (the server drops them).
        grants: level === "autonomous" ? [...otherGrants, ...(outwardOn ? ["outward"] : [])] : [],
      },
      {
        onSuccess: () =>
          toast.success(t("Autonomy saved", "Selbstständigkeit gespeichert"), {
            description: t(
              "It takes effect when you publish this version.",
              "Sie wirkt, sobald du diese Version veröffentlichst.",
            ),
          }),
        onError: (error) =>
          toast.error(t("Couldn't save autonomy", "Selbstständigkeit nicht gespeichert"), {
            description: error instanceof Error ? error.message : String(error),
          }),
      },
    );
  }

  return (
    <Panel className="scroll-mt-24 p-5" id={AUTONOMY_ANCHOR}>
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
        {t("how far it acts before asking", "wie weit er ohne Rückfrage handelt")}
      </div>
      <h3 className="mt-0.5 font-serif text-lg">{t("Autonomy", "Selbstständigkeit")}</h3>
      <p className="mt-1 text-xs text-muted-foreground">
        {fill(
          t(
            "Guardrails decide what {name} may do. This decides whether it does it without asking a person first.",
            "Guardrails legen fest, was {name} darf. Hier legst du fest, ob er es tut, ohne vorher einen Menschen zu fragen.",
          ),
          { name: agent.name },
        )}
      </p>

      <div role="radiogroup" className="mt-4 grid gap-2 sm:grid-cols-3">
        {LEVELS.map((option) => (
          <label
            key={option}
            className={`flex cursor-pointer flex-col gap-1 rounded-md border p-3 text-sm ${
              level === option ? "border-primary bg-primary/10" : "border-border"
            } ${mayManage ? "" : "cursor-not-allowed opacity-70"}`}
          >
            <span className="flex items-center gap-2 font-medium">
              <input
                type="radio"
                name={`autonomy-${agent.id}`}
                value={option}
                checked={level === option}
                disabled={!mayManage}
                onChange={() => setLevel(option)}
              />
              {labels[option].title}
            </span>
            <span className="text-xs text-muted-foreground">{labels[option].body}</span>
          </label>
        ))}
      </div>

      <label
        className={`mt-4 flex items-start gap-2 rounded-md border border-border p-3 text-sm ${
          level === "autonomous" && mayManage ? "cursor-pointer" : "opacity-60"
        }`}
      >
        <input
          type="checkbox"
          className="mt-0.5"
          checked={outwardOn}
          disabled={!mayManage || level !== "autonomous"}
          onChange={(e) => setOutward(e.target.checked)}
        />
        <span>
          <span className="font-medium">
            {t(
              "May reach people outside without asking",
              "Darf ohne Rückfrage nach außen kommunizieren",
            )}
          </span>
          <span className="mt-0.5 block text-xs text-muted-foreground">
            {level === "autonomous"
              ? t(
                  "Only for an autonomous agent: sends, replies and forwards go out without anyone approving each message.",
                  "Nur für einen selbstständigen Agenten: Senden, Antworten und Weiterleiten gehen raus, ohne dass jemand jede einzelne Nachricht freigibt.",
                )
              : t(
                  "Choose Autonomous first. Until then every message to the outside waits for a person.",
                  "Wähle zuerst Selbstständig. Bis dahin wartet jede Nachricht nach außen auf einen Menschen.",
                )}
          </span>
        </span>
      </label>

      {outwardOn && (
        <div className="mt-3 flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-xs">
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
          <span>
            {fill(
              t(
                "{name} will send mail and messages to customers and partners without a person approving each one. Guardrails and usage limits still apply, so set a daily limit on each tool that sends, so a mistake can't send hundreds of messages.",
                "{name} schickt dann Mails und Nachrichten an Kunden und Partner, ohne dass ein Mensch jede einzelne freigibt. Guardrails und Nutzungslimits gelten weiter. Setz deshalb an jedem Tool, das sendet, ein Tageslimit, damit ein Fehler nicht hunderte Nachrichten verschickt.",
              ),
              { name: agent.name },
            )}
          </span>
        </div>
      )}

      {mayManage && (
        <div className="mt-4 flex items-center justify-between gap-3">
          <span className="text-xs text-muted-foreground">
            {t(
              "Takes effect when you publish this version.",
              "Wirkt, sobald du diese Version veröffentlichst.",
            )}
          </span>
          <button
            type="button"
            onClick={onSave}
            disabled={!dirty || save.isPending}
            className="rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
          >
            {save.isPending ? t("Saving…", "Wird gespeichert…") : t("Save", "Speichern")}
          </button>
        </div>
      )}
    </Panel>
  );
}
