import { useState, type ReactElement } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { describeGuardrailSaveError } from "@/components/tool-guardrail-table";
import { LEVELS, levelLabel, type Level } from "@/lib/guardrail-levels";
import { useSetMyGuardrail } from "@/lib/hooks-member-guardrails";
import { useT } from "@/lib/i18n";
import { toolLabel } from "@/lib/member-guardrail-text";

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

function asLevel(v: unknown): Level | null {
  return LEVELS.includes(v as Level) ? (v as Level) : null;
}

// The Copilot's suggestion of a member guardrail (`guardrail_card`, Task 6):
// "Never send a mail to external addresses without me" becomes this card,
// and only the member's own Confirm saves it -- through their own
// `PUT /agents/{id}/guardrails/me/{toolKey}`. The Copilot never writes a
// guardrail itself. A level the card does not know renders as plain text
// with nothing to confirm (allowed is a reset, never a suggestion).
export function GuardrailCard({ props }: { props: Record<string, unknown> }): ReactElement {
  const t = useT();
  const agentId = str(props.agentId);
  const agentName = str(props.agentName);
  const toolKey = str(props.toolKey);
  const level = asLevel(props.level);
  const baseLevel = asLevel(props.baseLevel);
  const save = useSetMyGuardrail(agentId);
  const [state, setState] = useState<"open" | "saved" | "dismissed">("open");
  const [error, setError] = useState<string | null>(null);
  const valid = !!agentId && !!toolKey && level !== null && level !== "allowed";

  async function confirm() {
    if (!valid || level === null) return;
    setError(null);
    try {
      await save.mutateAsync({ toolKey, level });
      setState("saved");
    } catch (err) {
      setError(
        `${t("Couldn't save this rule.", "Diese Regel konnte nicht gespeichert werden.")} ${describeGuardrailSaveError(err, t)}`,
      );
    }
  }

  return (
    <Card className="space-y-2 p-3 text-sm">
      <div className="flex items-start justify-between gap-2">
        <div className="space-y-0.5">
          <div className="text-xs text-muted-foreground">
            {t("Your guardrail", "Deine Guardrail")}
            {agentName ? ` · ${agentName}` : ""}
          </div>
          <div className="font-medium">
            {level
              ? `${toolLabel(toolKey)} → ${levelLabel(level, "member", t)}`
              : toolLabel(toolKey)}
          </div>
        </div>
        {state === "saved" && <Badge variant="secondary">{t("Active", "Aktiv")}</Badge>}
      </div>
      {baseLevel && state === "open" && (
        <p className="text-xs text-muted-foreground">
          {t("Today", "Heute")}: {levelLabel(baseLevel, "member", t)}
        </p>
      )}
      {error && (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      )}
      {state === "dismissed" && (
        <p className="text-xs text-muted-foreground">{t("Left as it is.", "Bleibt wie es ist.")}</p>
      )}
      {state === "open" && valid && (
        <div className="flex flex-wrap gap-2 pt-1">
          <Button
            size="sm"
            className="min-h-11"
            onClick={() => void confirm()}
            disabled={save.isPending}
          >
            {t("Confirm", "Bestätigen")}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="min-h-11"
            onClick={() => setState("dismissed")}
            disabled={save.isPending}
          >
            {t("Not now", "Nicht jetzt")}
          </Button>
        </div>
      )}
    </Card>
  );
}
