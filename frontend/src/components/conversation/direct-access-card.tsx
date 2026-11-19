// The Copilot's direct access in the chat (copilot direct access §3, §8): the
// offer and every held write as a card in the turn, and a banner while a grant
// is active. Decided through the chat's own door -- the person needs no seat.
import { ExternalLink, KeyRound, X } from "lucide-react";
import { useState } from "react";

import { isHttpUrl, RecordLink } from "@/components/dashboard/widgets/approvals-widget";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api";
import {
  useDecideDirectAccess,
  useDirectAccessGrants,
  useRevokeDirectAccess,
} from "@/lib/direct-access-api";
import type { Approval } from "@/lib/hooks";
import { resolveTranslation, useLang, useT } from "@/lib/i18n";

import { isPlainClick, objectHref } from "./object-links";
import { useObjectNavigation } from "./use-object-navigation";

type Chosen = NonNullable<NonNullable<Approval["directAccess"]>["chosenAgent"]>;

/** The backend's typed refusal (`detail: {code}`), if the error is one. */
function refusalCode(error: Error): string | null {
  if (!(error instanceof ApiError)) return null;
  const detail = (error.body as { detail?: { code?: unknown } } | undefined)?.detail;
  return typeof detail?.code === "string" ? detail.code : null;
}

export function DirectAccessCard({ approval }: { approval: Approval }) {
  const t = useT();
  const { lang } = useLang();
  const info = approval.directAccess;
  const decide = useDecideDirectAccess();
  const revoke = useRevokeDirectAccess(info?.sessionId ?? "");
  const { data: grants } = useDirectAccessGrants(info?.sessionId ?? null);
  const [decided, setDecided] = useState<"approved" | "rejected" | "awaiting" | null>(null);
  const [started, setStarted] = useState<Chosen | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const openObject = useObjectNavigation();
  if (!info) return null;
  const agents = info.agents ?? [];
  const chosen = info.chosenAgent ?? started;
  const offersAgents = agents.length > 0 && !chosen;
  const isOffer = approval.actionType === "direct_access";
  // Person first, then the department (spec §1): the person's approval of a
  // write a department's policy holds leaves it waiting for that department.
  const status =
    decided ??
    (approval.status === "approved" && info.awaitingDepartment ? "awaiting" : approval.status);
  const grant = isOffer ? grants?.find((g) => g.approvalId === approval.id) : undefined;
  const title = offersAgents
    ? t(`${agents[0].name} does this — run it?`, `${agents[0].name} erledigt das – starten?`)
    : resolveTranslation(approval.title, approval.titleTranslations, lang);
  const account =
    info.account === "own"
      ? t("Using your own login", "Mit deinem eigenen Login")
      : info.account === "organisation"
        ? t("Using the organisation account", "Mit dem Organisationskonto")
        : t("The account could not be determined.", "Das Konto konnte nicht ermittelt werden.");
  // A refusal shown for an earlier answer must not stand next to a later
  // answer's outcome: cleared on every new answer and on its success.
  const answer = (decision: "approve" | "reject") => {
    setProblem(null);
    decide.mutate(
      { approvalId: approval.id, decision },
      {
        onError: (error) => setProblem(error.message),
        onSuccess: (dto) => {
          setProblem(null);
          setDecided(
            decision === "reject"
              ? "rejected"
              : dto?.directAccess?.awaitingDepartment
                ? "awaiting"
                : "approved",
          );
        },
      },
    );
  };

  const runAgent = (agent: { id: string; name: string }) => {
    setProblem(null);
    decide.mutate(
      { approvalId: approval.id, decision: "approve", agentId: agent.id },
      {
        onSuccess: (dto) => {
          setProblem(null);
          const next = dto?.directAccess?.chosenAgent;
          if (next) setStarted(next);
          else setDecided("approved");
        },
        onError: (error) =>
          setProblem(
            refusalCode(error) === "agent_not_runnable"
              ? t(
                  `You can no longer run ${agent.name}.`,
                  `Du darfst ${agent.name} nicht mehr starten.`,
                )
              : refusalCode(error)
                ? t(
                    "That did not work. Please try again.",
                    "Das hat nicht geklappt. Bitte versuch es erneut.",
                  )
                : error.message,
          ),
      },
    );
  };

  if (chosen) {
    const href = objectHref("run", chosen.runId);
    return (
      <div className="rounded-lg border border-border bg-card p-4 text-sm">
        <div className="flex items-center gap-2 font-medium">
          <KeyRound className="size-4 text-primary" aria-hidden />
          <span>{title}</span>
        </div>
        <p className="mt-2">
          <a
            href={href ?? undefined}
            onClick={(e) => {
              if (!href || !openObject || !isPlainClick(e)) return;
              e.preventDefault();
              openObject(href);
            }}
            className="text-primary hover:underline"
          >
            {t(`${chosen.name} is on it.`, `${chosen.name} kümmert sich darum.`)}
          </a>
        </p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-border bg-card p-4 text-sm">
      <div className="flex items-center gap-2 font-medium">
        <KeyRound className="size-4 text-primary" aria-hidden />
        <span>{title}</span>
      </div>
      {offersAgents && info.request && (
        <p className="mt-1 text-xs italic text-muted-foreground">
          {t("Hands over:", "Übergibt:")} “{info.request}”
        </p>
      )}
      <p className="mt-1 text-xs text-muted-foreground">{account}</p>
      {approval.detail && !(offersAgents && approval.detail === info.request) && (
        <p className="mt-2">{approval.detail}</p>
      )}
      {isOffer && (
        <p className="mt-2 text-xs text-muted-foreground">
          {t(
            "Reading is allowed for the rest of this chat. Every change asks you again.",
            "Lesen ist für den Rest dieses Chats erlaubt. Jede Änderung fragt dich erneut.",
          )}
        </p>
      )}
      {isHttpUrl(approval.recordUrl) && (
        <RecordLink href={approval.recordUrl}>
          <ExternalLink className="h-3.5 w-3.5 shrink-0" aria-hidden />
          {t("Open the record", "Datensatz öffnen")}
        </RecordLink>
      )}
      {approval.relatedLinks
        ?.filter((link) => isHttpUrl(link.url))
        .map((link) => (
          <RecordLink key={link.url} href={link.url}>
            ↗ {link.label}
          </RecordLink>
        ))}
      {isHttpUrl(approval.resultUrl) && (
        <RecordLink href={approval.resultUrl}>
          <ExternalLink className="h-3.5 w-3.5 shrink-0" aria-hidden />
          {t("Created record", "Angelegter Datensatz")}
        </RecordLink>
      )}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        {status === "pending" && info.decider === "member" && offersAgents && (
          <>
            <Button size="sm" disabled={decide.isPending} onClick={() => runAgent(agents[0])}>
              {t("Run agent", "Agent starten")}
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={decide.isPending}
              onClick={() => answer("approve")}
            >
              {t(
                `Do it myself with ${info.loginName || info.label}`,
                `Selbst machen mit ${info.loginName || info.label}`,
              )}
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={decide.isPending}
              onClick={() => answer("reject")}
            >
              {t("Reject", "Ablehnen")}
            </Button>
          </>
        )}
        {status === "pending" && info.decider === "member" && !offersAgents && (
          <>
            <Button size="sm" disabled={decide.isPending} onClick={() => answer("approve")}>
              {isOffer ? t("Allow", "Erlauben") : t("Approve", "Zustimmen")}
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={decide.isPending}
              onClick={() => answer("reject")}
            >
              {t("Reject", "Ablehnen")}
            </Button>
          </>
        )}
        {((status === "pending" && info.decider === "department") || status === "awaiting") && (
          <span className="text-xs text-muted-foreground">
            {t("Waiting for the department's approval.", "Wartet auf die Freigabe der Abteilung.")}
          </span>
        )}
        {status === "approved" && (
          <span className="text-xs text-muted-foreground">{t("Approved", "Zugestimmt")}</span>
        )}
        {status === "rejected" && (
          <span className="text-xs text-muted-foreground">{t("Rejected", "Abgelehnt")}</span>
        )}
        {grant && (
          <Button
            size="sm"
            variant="ghost"
            disabled={revoke.isPending}
            onClick={() => revoke.mutate(grant.id)}
          >
            {t("Revoke access", "Zugriff entziehen")}
          </Button>
        )}
      </div>
      {offersAgents && agents.length > 1 && status === "pending" && info.decider === "member" && (
        <div className="mt-2 flex flex-wrap items-center gap-1 text-xs text-muted-foreground">
          <span>{t("Also fits: ", "Passt auch: ")}</span>
          {agents.slice(1).map((agent) => (
            <Button
              key={agent.id}
              size="sm"
              variant="ghost"
              className="h-6 px-2 text-xs"
              disabled={decide.isPending}
              aria-label={t(`Run ${agent.name}`, `${agent.name} starten`)}
              onClick={() => runAgent(agent)}
            >
              {agent.name}
            </Button>
          ))}
        </div>
      )}
      {problem && (
        <p role="alert" className="mt-2 text-xs text-destructive">
          {problem}
        </p>
      )}
    </div>
  );
}

/** "Direct access: Odoo (read) · Revoke" while a grant is active (§8). */
export function DirectAccessBanner({ sessionId }: { sessionId: string }) {
  const t = useT();
  const { data: grants } = useDirectAccessGrants(sessionId);
  const revoke = useRevokeDirectAccess(sessionId);
  if (!grants || grants.length === 0) return null;
  return (
    <div
      role="status"
      className="inline-flex min-h-9 flex-wrap items-center gap-2 rounded-full border border-border bg-card pl-3 pr-1 text-sm"
    >
      <KeyRound className="size-4 text-primary" aria-hidden />
      {grants.map((g) => (
        <span key={g.id} className="inline-flex items-center gap-1">
          {t("Direct access", "Direktzugriff")}: {g.label} ({t("read", "lesen")}) ·
          <button
            type="button"
            onClick={() => revoke.mutate(g.id)}
            disabled={revoke.isPending}
            aria-label={`${t("Revoke", "Entfernen")} ${g.label}`}
            className="inline-flex items-center gap-1 rounded-full px-2 text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-60"
          >
            {t("Revoke", "Entfernen")}
            <X className="size-3.5" aria-hidden />
          </button>
        </span>
      ))}
    </div>
  );
}
